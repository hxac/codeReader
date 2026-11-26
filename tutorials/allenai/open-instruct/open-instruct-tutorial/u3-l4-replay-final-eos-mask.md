# 回放数据的末位 EOS 掩码：H270 修复与构建清单绑定

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 ledger H270 的问题：长上下文**回放文档**（replay，预训练式长文本）以 numpy 目录经 `--extra_numpy_dirs` 进入 GPU SFT 时，每个文档末尾的 EOS token 也被当作训练目标，教会模型「在超长上下文之后停止」。
2. 掌握判定规则：**不以 `<|im_start|>`（id 100264）开头的文档是回放文档**（聊天行都以它开头）；只有「长度 ≥ 2、不以 im_start 开头、以 EOS（id 100257）结尾」的文档，其 `end - 1` 位置的 label 被清掉——聊天行、以其他 token 结尾的文档、文档内部的 EOS、单 token 文档都不受影响。
3. 读懂 `replay_final_eos_positions` 这个纯函数，并理解它为什么要求 `document_bounds` 是**整文档边界**：按 EOS 扫描或按 part 截断都会把内部 EOS 误认成文档末尾。
4. 会用 `scripts/data/mask_replay_final_eos.py` 写掩码目录：以回放构建的 `manifest.jsonl` 为文档边界的唯一来源，新写 label mask 分 part、symlink token 分 part 与 `.csv.gz` 与 manifest（打包与数据顺序不变），重算 `dataset_statistics.json`，并理解它的一串防错校验（sha256 锁清单、csv 行与 manifest 一致、拒绝跨 part 文档、拒绝孤儿 part、拒绝已存在的输出目录）。
5. 会用 `scripts/data/check_replay_eos_mask.py` 做独立核对：不依赖 writer 的任何代码，直接读真实数据按模式核对、支持 `--output` JSON、任何差异退出码为 1，并能说明它为什么是「机制检查」而非训练效果的证明。

## 2. 前置知识

本讲站在 u3-l2（numpy 缓存三件套）和 u7-l1（`olmo_core_finetune.py` 的缓存先行工作流）之上，先温习四个概念。

**label mask（标签掩码）。** numpy 缓存里每个 token 有一个 `labels_mask` 字节：1 = 可训练，0 = 掩掉。关键语义是：**mask 按目标 token 索引**——`mask[i] = 1` 表示「第 i 个 token 作为预测目标参与 loss」，也就是「用第 i 个 token 之前的上文去预测第 i 个 token」。SFT 的聊天缓存里 prompt 部分 mask 为 0、assistant 回答部分为 1（见 u2-l3）；而预训练式数据通常是**全 1**——每个 token 都要预测。

**numpy 目录三件套。** 转换器写出的布局是 `token_ids_part_XXXX.npy`（token id，uint8/16/32/64）+ `labels_mask_part_XXXX.npy`（每 token 一字节）+ `token_ids_part_XXXX.csv.gz`（每行一个 `start,end` 文档边界，行与行首尾相接、铺满整个文件），外加 `dataset_statistics.json`。目录头部注释见 [open_instruct/numpy_dataset_conversion.py:3-9](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L3-L9)（定义了这套布局与各文件的含义）。

**`--extra_numpy_dirs`。** OLMo-core SFT 支持在聊天缓存之外并列训练若干本地 numpy 目录，字段说明在 [open_instruct/olmo_core_finetune.py:468-480](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/olmo_core_finetune.py#L468-L480)（要求每个目录用转换器布局、多 part 目录在元数据边界模式下必须带 `--row_aligned_parts` 标记、超长文档会被截断）。回放目录正是从这里进来的。

**row_aligned_parts 标记。** 多 part 目录若在 part 中间把一行切断，元数据边界会把同一行训练成两个文档。所以训练启动时用 `_check_row_aligned_cache` 强制要求 `dataset_statistics.json` 里 `row_aligned_parts: true` 且元数据行数等于总行数，见 [open_instruct/olmo_core_finetune.py:114-129](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/olmo_core_finetune.py#L114-L129)（启动期校验行对齐缓存：标记存在、元数据行数 == total_instances）。本讲的输出目录必须自己写出这个标记。

最后是两个 token：Olmo 3.5 的分词器（allenai/dolma2-tokenizer-olmo35）里 **EOS 是 100257**——它同时也是每个 assistant 回合的结束符；**`<|im_start|>` 是 100264**——每条聊天行的第一个 token。这两个 id 常量定义在 [open_instruct/numpy_dataset_conversion.py:644-646](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L644-L646)（声明 EOS 与 im_start 的 token id，注释说明 EOS 也结束每个 assistant 回合）。记住这层对偶：**EOS 既出现在聊天回合末尾（这时必须训练，模型才知道何时收尾），也出现在回放文档末尾（这时不能训练，否则模型学会「长文本后停机」）**——整个修复就是在区分这两种 EOS。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [open_instruct/numpy_dataset_conversion.py](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py) | 核心库：token id 常量、`replay_final_eos_positions` 判定纯函数、`write_replay_eos_masked_dir` 写目录，以及 `_numpy_dir_parts` / `_read_document_bounds` / `_replay_manifest_bounds` 三个辅助函数（L644-851） |
| [scripts/data/mask_replay_final_eos.py](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/mask_replay_final_eos.py) | 写掩码目录的 CLI：调用上面的 writer，随后跑训练侧启动检查并对账 |
| [scripts/data/check_replay_eos_mask.py](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py) | 独立核对 CLI：只用 numpy + 标准库，直接读真实数据核对掩码 |
| [open_instruct/test_numpy_dataset_conversion.py](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py) | writer 与判定函数的单元测试：玩具目录构造器 `_write_replay_dir`、9 文档判定用例、逐条拒绝路径用例、打包级端到端用例（L839-1085） |
| [tests/test_check_replay_eos_mask.py](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py) | 核对脚本的测试：先用 writer 写出正确目录，再逐类破坏并断言计数器（全文） |
| [open_instruct/olmo_core_finetune.py](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/olmo_core_finetune.py) | 训练侧消费方：`--extra_numpy_dirs` 的启动校验（本讲只读它的 `_check_extra_numpy_dir` 与 `_check_row_aligned_cache`） |

## 4. 核心概念与源码讲解

### 4.1 回放文档判定与 H270 规则

#### 4.1.1 概念说明

长上下文 SFT 的常见配方是：聊天数据之外，混入一批**回放文档**——预训练式的长文本（每篇几万 token），让模型在后训练阶段继续见到长上下文。GPU 路线上它们以预构建 numpy 目录的形式经 `--extra_numpy_dirs` 与聊天缓存并列训练。

问题在于：回放构建按预训练惯例把整个目录的 label 掩码**全部置 1**。于是每个回放文档末尾的 EOS 也成了训练目标——loss 里出现了一条「读了几万个 token 的预训练文本之后，下一个该输出 EOS」的监督。模型从这里学到的是 \( P(\text{EOS} \mid \text{超长上下文}) \) 偏高，推理时表现为**在很长的上下文里提前停机**。这就是 ledger H270 记录的故障，脚本文档串里一句话概括了因果：

> A long-context replay dir (pretraining-style documents, each ending in EOS, passed to olmo_core_finetune.py with --extra_numpy_dirs) trains that final token as a target, which teaches the model to stop after a long context.

见 [scripts/data/mask_replay_final_eos.py:1-15](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/mask_replay_final_eos.py#L1-L15)（CLI 文档串：说明 SRC/OUT 的含义、判定规则与「用 OUT_DIR 替换 SRC_DIR 训练」的用法）。

修复的难点是**区分两种 EOS**：聊天行的末尾 EOS 必须留着训练（那是模型学会收尾的地方），回放文档的末尾 EOS 必须掩掉。区分的依据是文档的**第一个 token**：仓库里所有聊天行都以 `<|im_start|>` 开头，而回放文档是裸文本、不以它开头。据此得到 H270 的判定规则——

\[ \text{清掉 } d \text{ 的末位 label} \iff |d| \ge 2 \;\land\; d_0 \ne \text{im\_start} \;\land\; d_{|d|-1} = \text{EOS} \]

三个条件各防一种误伤：

- **不以 im_start 开头**：聊天行（含多轮行）全部豁免，assistant 回合的停止监督原样保留；
- **以 EOS 结尾**：被截断等原因导致末尾不是 EOS 的文档豁免——它本来就没有「末位 EOS」可清；
- **长度 ≥ 2**：单 token 文档豁免——它唯一 token 的目标要从**上一个文档**的末尾预测，属于跨文档目标，打包后根本不会被训练，清它既无意义也不对。

注意「清掉」的对象是 **label（监督）**，不是 token：EOS 本身留在 token 流里一动不动（4.5 会讲为什么必须如此）。另外这条规则与 TPU 侧同名修复（MaxText 的 `replay_eos_keep` patch）语义一致——本讲是它的 GPU 数据侧等价物，docstring 里称为「ledger H270's fix as data」。

#### 4.1.2 核心流程

从「一份回放目录」到「一份修好的回放目录」的全链路：

1. **构建期**（本仓库之外，ledger 的 `experiments/c11/c11_replay.py` build）：写出 `token_ids_part_*.npy`、全 True 的 `labels_mask_part_*.npy`、`token_ids_part_*.csv.gz`，以及 `manifest.jsonl`——每个文档一行，记录 `part` / `start` / `end` / `length_with_eos`（还有 `id` / `tokens_sha256`）。
2. **修数据**：`mask_replay_final_eos.py SRC OUT` 依据 manifest 的文档边界选出每个回放文档末位 EOS 的位置，写出一个新目录 OUT：mask 按位清零，其余文件 symlink 自 SRC。
3. **训练**：把命令行里的 `--extra_numpy_dirs` 从 SRC 换成 OUT。token、边界、打包、数据顺序完全不变，只有那些 label 位变了。
4. **核对**：`check_replay_eos_mask.py` 独立读 SRC/OUT 的真实数据逐项核对。

#### 4.1.3 源码精读

token id 常量与构建清单的名字、以及 H182 那次构建的 manifest sha256 钉子：

```python
OLMO35_EOS_TOKEN_ID = 100257
OLMO35_IM_START_TOKEN_ID = 100264
REPLAY_MANIFEST = "manifest.jsonl"
H182_REPLAY_MANIFEST_SHA256 = "2683f40911e79e95bae4d2202ada1b7f677b2b8daacf649b25ee929cd8dcc412"
```

见 [open_instruct/numpy_dataset_conversion.py:644-652](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L644-L652)（定义两个 token id、manifest 文件名常量，并注释 H182 清单的 sha256 与其来源路径、校验时间）。`H182_REPLAY_MANIFEST_SHA256` 是 CLI 的默认 `--expected_manifest_sha256`——换一份别的回放目录就要显式传新值。

测试里的玩具世界把这两个 id 换成了小数字（`_R_EOS, _R_IM = 2, 3`，词表 100,352 所以 token 用 uint32 存，和 Olmo 一致），并提供了两个构造器：[open_instruct/test_numpy_dataset_conversion.py:839-846](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L839-L846)（定义玩具 token id，并声明 `_write_replay_dir` 按构建脚本的方式写目录）与 [open_instruct/test_numpy_dataset_conversion.py:843-876](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L843-L876)（`_write_replay_dir` 的实现：逐 part 写 token/mask/csv，按文档算 `tokens_sha256` 写 manifest）。聊天行的构造器 `_chat_row` 见 [open_instruct/test_numpy_dataset_conversion.py:887-889](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L887-L889)（构造一条聊天行：前缀 4 个 token 不训练，内容加结尾 EOS 训练）——注意它的形状：`[IM, 10, 11, EOS]`，前 4 个（prompt 到 im_start 为止）mask 为 0，内容与 EOS 为 1。

#### 4.1.4 代码实践

**实践目标**：把判定规则内化成「看一眼文档就能说出会不会被清」。

**操作步骤**：

1. 打开 [open_instruct/test_numpy_dataset_conversion.py:893-910](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L893-L910)（`test_selects_only_the_final_eos_of_replay_documents`：9 个玩具文档一次覆盖所有边界情形）。
2. 准备一张三列表格：`文档` / `是否被规则选中` / `理由（对应规则的哪个条件）`。
3. 逐个填写那 9 个文档：聊天行 `[IM,10,11,EOS]`、回放 `[20,21,EOS]`、带内部 EOS 的 `[22,EOS,23,EOS]`、非 EOS 结尾的 `[24,25]`、单 token 的 `[EOS]` 和 `[26]`、两 token 的 `[27,EOS]`、多轮聊天行、以及只有一个 im_start 的 `[IM]`。
4. 对照断言 `positions.tolist() == [ends[1]-1, ends[2]-1, ends[6]-1]` 检查你的答案。

**需要观察的现象**：三个被选中的位置分别来自第 2、3、7 个文档；第 8 个多轮聊天行**一个都不选**——它内部有两个回合末尾的 EOS，全部保留。

**预期结果**：被选中的恰好是「回放 + EOS 结尾 + 长度 ≥ 2」的三个文档；`(tokens[positions] == EOS).all()` 成立。测试是现成的断言，你只是在纸上复算它。

#### 4.1.5 小练习与答案

**练习 1**：文档 `[7, EOS, 8, EOS]`（回放，内部含 EOS）会被清哪个位置？内部那个 EOS 呢？

答案：只清 `end - 1`，即**最后一个** EOS 的 label；内部那个 EOS 的 label 保留。内部 EOS 处的前文是文档自己的内容，它是合法的内容监督；「教会停机」的监督只来自文档末位那个 EOS。

**练习 2**：为什么不干脆把回放文档末尾的 EOS **token 从数据里删掉**，一步到位？

答案：token 流是打包、文档边界、数据顺序与所有校验哈希的公共基础。删一个 token 会让其后所有偏移移位、manifest 与 `.csv.gz` 全部作废、`tokens_sha256` 全部失配；在 EOS 扫描边界模式下 EOS 还是文档分隔符，删了它文档会互相粘连。而清 label 只翻转监督位，token 一位不动——这是「最小干预」：修的只是 loss，不是数据本身。

**练习 3**：聊天缓存的 mask 本来就有 0（prompt 部分），为什么回放目录的 mask 是全 1 也不算 bug、却偏偏末位 EOS 出事？

答案：回放数据按预训练惯例「每个 token 都是目标」，全 1 是**有意为之**——预训练式文本本来就该整段预测。出事的不是全 1 本身，而是「以 EOS 结尾的文档 + 全 1」这个组合：它把「何时停止」的监督塞进了本该只学内容的长文本里。聊天行的回合末 EOS 是我们**想要**的停止监督，回放文档的末位 EOS 是**不想要**的，所以修复按开头 token 区分二者。

### 4.2 replay_final_eos_positions：判定核心的纯函数

#### 4.2.1 概念说明

[open_instruct/numpy_dataset_conversion.py:663-682](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L663-L682)（`replay_final_eos_positions`：输入 token 数组与文档边界数组，输出要清的 label 位置）是整个修复的核心。它被设计成**纯函数**：numpy 数组进、numpy 数组出，无任何文件 IO。这个设计有两个直接好处：

1. 可以单独用一组玩具文档测试判定的每个分支（4.1.4 做的事）；
2. writer 与任何外部核对者都能对同一份数据独立复算它——4.4 的核对脚本干脆**不复用**它，而是按构建清单自己重推期望结果，两边对照。

它的 docstring 还写明了本讲最关键的一句话：mask 按目标 token 索引，所以在 `end - 1` 处清位，删掉的恰好是「由同一文档前一个 token 预测该 EOS」的那个目标——这正是 H270 要去掉的那条监督。

#### 4.2.2 核心流程

函数三步走（全部向量化，无 Python 循环）：

```
输入: token_ids[T], document_bounds[N,2]（每行 [start, end)，整文档边界）
1. 长度过滤:  ends - starts >= 2          # 丢掉单 token 文档
2. 选择:      token_ids[starts] != im_start  且  token_ids[ends-1] == eos
3. 输出:      ends[selected] - 1          # 每个入选文档一个位置
```

为什么输入必须是**整文档边界**？docstring 的最后一行点破：「an EOS-scan or a part cut would make an interior EOS look final」。两种错误的边界来源：

- **EOS 扫描**（`--no_document_boundaries_from_metadata` 模式的做法）：把文档内每个 EOS 都当成边界，于是内部 EOS 会被切成「伪文档的末位 EOS」，进而被清掉合法的内容监督；
- **part 截断**：把 part 文件尾巴当成文档末尾，截断点附近的 token 会被误判。

所以调用方（writer）必须传入**构建清单给出的整文档边界**，函数本身不做任何边界推断。

#### 4.2.3 源码精读

```python
def replay_final_eos_positions(token_ids, document_bounds, eos_token_id, im_start_token_id):
    starts, ends = document_bounds[:, 0], document_bounds[:, 1]
    long_enough = ends - starts >= 2
    starts, ends = starts[long_enough], ends[long_enough]
    selected = (token_ids[starts] != im_start_token_id) & (token_ids[ends - 1] == eos_token_id)
    return ends[selected] - 1
```

见 [open_instruct/numpy_dataset_conversion.py:678-682](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L678-L682)（函数体：先按长度过滤，再用花式索引同时检查每个文档的首 token 与末 token，返回入选文档的 `end - 1`）。注意两个细节：

- `[start, end)` 是左闭右开区间，文档最后一个 token 的下标是 `end - 1`，因此返回值是 `ends - 1`；
- `token_ids[starts]` 与 `token_ids[ends - 1]` 是对**同一批文档**并行的两次花式索引，之后按位与——一次向量化完成全部判定。

配套测试 [open_instruct/test_numpy_dataset_conversion.py:892-910](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L892-L910)（`TestReplayFinalEosPositions`：9 个文档拼接成一条 token 流，用 `np.cumsum` 手工拼出边界，断言选中的正是那三个位置且选中位置的 token 全是 EOS）。

#### 4.2.4 代码实践

**实践目标**：用一个手工小例子完整推演一遍「token 分 part + 文档边界 → 选中位置」。

**操作步骤**：拿出纸笔，构造**两个 part、三个文档**（玩具 id 沿用测试：`IM = 3`、`EOS = 2`）：

- part `0000`：文档 A = 聊天行 `[IM, 10, 11, EOS]`（start=0, end=4）；文档 B = 回放 `[20, 21, 24]`（start=4, end=7，注意它**不以 EOS 结尾**）；
- part `0001`：文档 C = 回放 `[30, 31, EOS]`（start=0, end=3）。

对每个 part 分别模拟函数三步：

1. part 0000 的 token 流是 `[3,10,11,2,20,21,24]`，边界 `[[0,4],[4,7]]`。长度过滤：4≥2、3≥2 都保留。选择：`tokens[0]=3` 等于 IM → A 出局；`tokens[4]=20` 不等于 IM，但 `tokens[7-1]=24` 不等于 EOS → B 出局。**返回空**。
2. part 0001 的 token 流是 `[30,31,2]`，边界 `[[0,3]]`。`tokens[0]=30` 不是 IM、`tokens[3-1]=2` 是 EOS → 选中，**返回 `[2]`**。
3. 串起来：整个目录只有 part 0001 的下标 2 这一个位置会被清。若源 mask 全 True，则 `dropped = 1`；part 0000 的输出 mask 与源逐字节相同。

**需要观察的现象**：聊天行 A 的末位 EOS 完好保留（它是要训练的停止监督）；B 因为结尾不是 EOS，什么也不发生；只有 C 的末位 EOS label 被清。

**预期结果**：positions = `{0000: [], 0001: [2]}`。把这个结果记下来——4.4 的综合实践会追问「如果这份目录喂给 check 脚本会怎样」，答案会出乎意料（见 4.4.5 练习 3）。

**待本地验证**：如果想跑真实代码而不是纸面推演，可以写十行示例代码（示例代码，非项目原有）用 `numpy` 拼出上面两个 part 的数组并调用 `replay_final_eos_positions`——本环境未执行，结果以你的本地运行为准。

#### 4.2.5 小练习与答案

**练习 1**：tokens = `[3,10,2, 20,2, 30,2]`，bounds = `[[0,3],[3,5],[5,7]]`。求 positions。

答案：文档 0 `[3,10,2]` 以 IM 开头 → 排除。文档 1 `[20,2]`：长 2、`tokens[3]=20` 不是 IM、`tokens[4]=2` 是 EOS → 选中，位置 `5-1=4`。文档 2 `[30,2]` 同理 → 位置 `7-1=6`。positions = `[4, 6]`，且 `tokens[4] = tokens[6] = 2`。

**练习 2**：如果有人把 bounds 换成 EOS 扫描的结果，`[22, EOS, 23, EOS]` 这个回放文档会出什么事？

答案：扫描会在内部 EOS 处把它切成 `[22,EOS]` 与 `[23,EOS]` 两段，两段都满足规则，于是**两个** EOS 的 label 都被清。内部 EOS 的监督是文档自身的内容，被错删；这就是 docstring 说「must be whole documents」的原因，也是 4.3 里把边界钉死到构建清单的原因。

**练习 3**：函数为什么自己不去读 `.csv.gz` 或 manifest，而非要调用方传边界进来？

答案：职责分离。判定函数只负责「给定边界，选哪些位置」；「边界从哪来、可不可信」是 writer 的安全责任（sha256 钉子 + 三方一致校验）。纯函数化还让它可以直接被单测覆盖，而不必先在磁盘上造假目录——`TestReplayFinalEosPositions` 全程没有碰文件系统。

### 4.3 mask_replay_final_eos.py 与构建清单：写掩码目录

#### 4.3.1 概念说明

writer 要解决的问题是**可信地知道每个文档的边界**。回放目录里的 `.csv.gz` 是训练真正读的边界，但它本身只是普通文件，没有任何东西证明「这就是构建当时写下的边界」。如果有人（或某个脚本）改写过 manifest 和 `.csv.gz`，让它们在内部 EOS 处把文档切开，两份文件会互相一致、却共同偏离构建事实——按它们清位就会错删内容监督。

所以 writer 把边界绑定到**构建清单**（build manifest）：`manifest.jsonl` 是回放构建逐文档写下的记录，而清单的可信度由**外部的 sha256 钉子**保证——CLI 默认要求它等于 `H182_REPLAY_MANIFEST_SHA256`（H182 那次构建的清单哈希，常数见 4.1.3）。writer 的 docstring 说得很直白：清单「must equal the .csv.gz rows and keep every document inside one part」，见 [open_instruct/numpy_dataset_conversion.py:739-757](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L739-L757)（writer 的 docstring：声明 sha256 前置校验、清单必须与 csv 行一致且文档不跨 part、输出目录的构成与「源的统计不随迁」）。

#### 4.3.2 核心流程

writer `write_replay_eos_masked_dir` 分两大阶段，**先全盘校验、后落盘**，任何拒绝都发生在写任何字节之前：

```
SRC_DIR
├─ manifest.jsonl ──(存在性 + sha256 == 钉子)──> _replay_manifest_bounds
│                                              → {part 索引: (start,end) 行}
├─ token_ids_part_*.npy ─┐
├─ labels_mask_part_*.npy ┼─(四套索引集合必须一致: tokens/mask/csv/manifest)
├─ token_ids_part_*.csv.gz ┘
└─ 逐 part 校验(循环,全部通过才继续):
     1. mask 字节数 == token 数,token 字节数是它的 1/2/4/8 倍
     2. manifest 行铺满 part:首行 start=0,行间首尾相接,末行 end=token 数
     3. csv 行与 manifest 行逐行相等
     4. replay_final_eos_positions(tokens, bounds) → 该 part 的待清位置
     (收集进 plan;注释:"Positions first, so nothing is written for a dir that is refused.")
OUT_DIR = os.makedirs(out_dir)      # 已存在 → FileExistsError
├─ labels_mask_part_*.npy   每个 part:复制源 mask → 位置清 0 → flush
├─ token_ids_part_*.npy ┐
├─ token_ids_part_*.csv.gz ┼─ symlink → SRC 同名文件
├─ manifest.jsonl       ┘
└─ dataset_statistics.json  新写(见 4.5)
```

CLI 侧（`mask_replay_final_eos.py` 的 `main`）在写完之后立刻执行训练侧启动检查并对账，见 [scripts/data/mask_replay_final_eos.py:38-54](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/mask_replay_final_eos.py#L38-L54)（调用 writer 后，用 `oc_data.TokenizerConfig.dolma2()` 与 OLMo-core 的 dtype 规则跑 `_check_extra_numpy_dir`，再跑 `_check_row_aligned_cache`，若启动检查数出的 loss token 数与统计不符则抛错，最后打印变更摘要与 PASS 行）。

#### 4.3.3 源码精读

**前置校验（先于一切写入）**：[open_instruct/numpy_dataset_conversion.py:759-771](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L759-L771)（源目录 realpath 化、manifest 不存在即报错、按 16 MiB 块流式算 sha256、与钉子不符则报错——报错文本明确说「its boundaries cannot be tied to the replay build」）。sha256 在读任何 part 之前算完，这一步是整条信任链的锚点。

**索引集合一致性**：[open_instruct/numpy_dataset_conversion.py:772-780](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L772-L780)（用 `_numpy_dir_parts` 列出 tokens/mask/metadata 三类文件，与 manifest 的 part 集合比对，四者不等则报错并列出各集合）。孤儿文件（如多余的 `labels_mask_part_0001.npy`）、缺 part、以及「一个文档横跨两个 part」的清单都会在这里现形——跨 part 文档的 manifest 只有一个 part 号，索引集合自然对不上。

**逐 part 校验与位置计划**：[open_instruct/numpy_dataset_conversion.py:781-803](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L781-L803)（对每个 part：从 mask 文件字节数推出 token 数、验证 token 文件 dtype 是 uint8/16/32/64 的整倍；检查 manifest 行铺满该 part（首行起点 0、行间无缝、末行终点等于 token 数）；用 `_read_document_bounds` 读 `.csv.gz` 并要求与 manifest 行逐行相等；然后 `replay_final_eos_positions` 算出该 part 的待清位置，同时累计 documents/starting_im_start/not_ending_eos 三个透明度计数）。其中：

- `_numpy_dir_parts` 见 [open_instruct/numpy_dataset_conversion.py:685-693](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L685-L693)（用三个 glob 的正则把目录里的 part 文件按类别收进 `{类别: {索引: 路径}}`）；
- `_read_document_bounds` 见 [open_instruct/numpy_dataset_conversion.py:696-706](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L696-L706)（逐行解析 `.csv.gz` 为 `(start, end)`，**空行或非数字行直接抛错**——注释解释了原因：OLMo-core 解析不了它，而启动检查还会把它数成一行）；
- `_replay_manifest_bounds` 见 [open_instruct/numpy_dataset_conversion.py:709-729](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L709-L729)（manifest 缺失即报错；逐行读 `part/start/end`，若行有 `length_with_eos` 字段则要求 `end - start` 与它相等，矛盾即抛错；按 part 索引归组返回）。

**写阶段**：[open_instruct/numpy_dataset_conversion.py:805-821](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L805-L821)（`os.makedirs(out_dir)` 对已存在目录抛 `FileExistsError`；每个 part 复制源 mask 文件、以 `r+` memmap 打开、把 plan 里的位置写 0、`flush`，统计 `was_trained` 得出 dropped 与 already_untrained 两数；随后把 token part 与 `.csv.gz` part symlink 过来，最后 symlink manifest）。symlink 而不是复制，是「打包不变性」的机械保证（4.5）。

CLI 入口：[scripts/data/mask_replay_final_eos.py:25-54](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/mask_replay_final_eos.py#L25-L54)（`main`：位置参数 src/out，`--expected_manifest_sha256` 默认 H182 常量，`--max_seq_length` 供启动检查的截断计数，`--eos_token_id` / `--im_start_token_id` 默认 Olmo 3.5 的两个 id；写完后跑训练侧同一套校验并做 loss token 对账）。

**拒绝路径一览**（每条都有一条测试钉住）：

| 拒绝路径 | 触发条件 | 报错关键词 | 测试 |
| --- | --- | --- | --- |
| 缺构建清单 | src 没有 `manifest.jsonl` | `has no manifest.jsonl` | [test_numpy_dataset_conversion.py:1017-1020](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L1017-L1020) |
| 清单不是钉住的那份 | sha256 不匹配（哪怕 manifest 与 csv 互相一致） | `not the pinned` | [test_numpy_dataset_conversion.py:997-1007](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L997-L1007) |
| 索引集合不一致 | tokens/mask/csv/manifest 四方索引不同（孤儿 part、缺 part、文档跨 part） | `one shared index set` | [test_numpy_dataset_conversion.py:1030-1041](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L1030-L1041) |
| csv 行与清单不一致 | 比如按 EOS 扫描的行在内部 EOS 处切开 | `differ from the manifest` | [test_numpy_dataset_conversion.py:1022-1028](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L1022-L1028) |
| csv 空行/畸形行 | 非法行直接拒绝 | `not a start,end row` | [test_numpy_dataset_conversion.py:1009-1015](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L1009-L1015) |
| 输出目录已存在 | `os.makedirs` 不覆盖 | `FileExistsError` | [test_numpy_dataset_conversion.py:1043-1049](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L1043-L1049) |

另外两条代码内校验没有专属测试用例（属相邻用例的覆盖空隙，读代码时留意）：manifest 行不铺满 part（`do not tile`，[numpy_dataset_conversion.py:789-795](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L789-L795)：检查首行起点、行间无缝、末行终点）与 `length_with_eos` 与 `start/end` 矛盾（[numpy_dataset_conversion.py:724-727](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L724-L727)：一行 span 与记录的长度不等即抛错）。

#### 4.3.4 代码实践

**实践目标**：把 writer 的拒绝路径与测试一一对应，体会「fail-closed」的设计。

**操作步骤**：

1. 依次阅读上表右列的五个测试用例，每条记录：测试如何构造坏目录、断言的报错正则、以及断言 OUT 目录**不存在**（`_refused` 辅助函数统一做了后一件事，见 [test_numpy_dataset_conversion.py:992-995](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L992-L995)（`_refused`：断言抛出匹配的 ValueError 且输出目录没有被创建））。
2. 重点读 [test_numpy_dataset_conversion.py:997-1007](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L997-L1007)（`test_refuses_a_manifest_that_is_not_the_pinned_one`）：测试把 `[44, EOS, 55, EOS]` 的 manifest 与 csv **同时**改写成内部 EOS 处切开的两行——两份文件完全互相一致，唯一能戳穿它的是 sha256 钉子。
3. 读 [test_numpy_dataset_conversion.py:936-965](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L936-L965)（`test_clears_exactly_the_replay_final_eos_labels`：两个 part 的混合目录，断言 SRC 逐字节未动、每个 part 恰好在预期位置翻转、token/csv/manifest 都是 symlink、源的 `stats.json` 不随迁、`dropped/already_untrained/documents` 计数、以及 `trainable_tokens` 的算术）。
4. 可选验证（**待本地验证**，需要 dev 依赖里的 torch 与 olmo-core）：`uv run pytest open_instruct/test_numpy_dataset_conversion.py -k Replay -v`。本环境未执行，不要假设其输出。

**需要观察的现象**：所有拒绝用例中 OUT 都没被创建；而正向用例中 SRC 的字节完全没变、symlink 的 `realpath` 指回 SRC。

**预期结果**：你会得出结论——writer 的输出要么完整存在、要么根本不存在，没有「写了一半」的中间态。

#### 4.3.5 小练习与答案

**练习 1**：为什么所有校验（sha256、索引集合、铺满、csv 一致）必须发生在 `os.makedirs(out_dir)` 之前？

答案：让拒绝零副作用。数据是长跑训练的输入，一个半成品或错误目录被下游误用的代价远高于重跑一次脚本；代码里 `# Positions first, so nothing is written for a dir that is refused.` 的注释把这条原则写在了计划循环的位置上。`os.makedirs` 不带 `exist_ok` 则把「输出目录必须全新」也纳入同一原则。

**练习 2**：`--expected_manifest_sha256` 换一份新的回放目录时怎么用？钉子为什么放在 CLI 而不是库函数里写死？

答案：新目录的构建方需要提供自己 manifest 的 sha256，显式传给 `--expected_manifest_sha256`（或改默认值，像 H182 那样作为常量入库）。钉子本质是「构建事实的指纹」，它属于**运维层**的一次性约定，而 `write_replay_eos_masked_dir` 是通用库函数——把信任锚做成参数，函数才能服务任意构建，测试也才能对玩具目录「自钉」（`_masked` 辅助函数现场算 sha256，见 [test_numpy_dataset_conversion.py:922-928](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L922-L928)（测试辅助：默认用清单当前内容的 sha256 当钉子，或显式传入另一个））。

**练习 3**：源目录里那个 `stats.json` 为什么**不能**随迁到 OUT？

答案：它记录的是修复**之前**的统计（比如全部 token 可训练），对 OUT 来说是错的。writer 的统计必须按新 mask 重算（4.5）；测试明确断言 `stats.json` 不存在于 OUT，见 [test_numpy_dataset_conversion.py:959](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L959)（断言 OUT 里没有源的 stats.json，注释说明源的计数在 OUT 中已失效）。

### 4.4 check_replay_eos_mask.py：独立核对

#### 4.4.1 概念说明

writer 说自己写对了，不算数。核对脚本的设计原则写在它的 docstring 第二段：**只用 numpy 和标准库，不碰任何写出 OUT 的代码**，见 [scripts/data/check_replay_eos_mask.py:1-28](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L1-L28)（CLI 文档串：说明用法、独立实现的原则、五组检查与 PASS 条件）。更关键的一句是「Replay documents are found from the replay build's inventory, **not from the rule under test**」——**期望值不来自被测的规则，而来自构建的独立记录**：manifest 的 sha256 + 来自构建 R2 回执的 `ids_sha256` / `rows` / `tokens` 四个钉子（PINS），见 [scripts/data/check_replay_eos_mask.py:43-50](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L43-L50)（`h182` 的四元组钉子：清单 sha、id 列表 sha、行数 58,480、token 总数 2,678,796,581，文档串注明其与 ledger 的 r2_b1c12r.json 回执对应）。

所以它是一个「**机制检查**」（mechanism check）：它验证的是**数据变换这个机制**在真实数据上成立——每个清单文档都在声明的位置、内容哈希吻合、末位 label 在 OUT 中未训练、其他 label 一处不多一处不少、token 与边界原封未动、统计与启动检查的读法一致。它**不**证明「训练后的模型不再提前停机」——那是评测（checkpoint 质量评估）的事；也不复述 writer 的规则，而是从清单独立重推期望，因此**writer 规则写反、漏选、多选、写错文件**任何一种系统性错误都会被它抓到。用一句话：writer 与 checker 是两条独立实现 + 一份共同外部事实（构建回执），互为审计。

#### 4.4.2 核心流程

docstring 列的五组检查，加上聊天目录与统计两块：

1. **文件层面**：SRC 与 OUT 各自的 token/mask/`.csv.gz` part 共享同一索引集合（等于清单的 parts），无孤儿 part 文件；OUT 的 token part、`.csv.gz` part 与 manifest 就是 SRC 的（realpath 相同或 sha256 相同）。
2. **边界层面**：每个 part 的清单行铺满它、且等于该 part `.csv.gz` 的行——训练读到的边界与清单一致，没有文档被切在两个 part 之间。
3. **清单层面**：每个清单文档恰好在它声明的位置被找到——长度等于 `length_with_eos`、末 token 是 EOS、前 `n-1` 个 token 的哈希等于 `tokens_sha256`；id 无重复、行数与 token 总数与钉子相符。
4. **掩码层面**：每个清单文档的末位 label 在 OUT 中未训练（SRC 训练过的计为 `dropped_at_final`，本来就没训练的计为 `already_untrained_at_final`，两者之和必须等于钉住的行数）；除此之外**没有别的 label 被清**（`cleared_elsewhere`）、**没有 label 被加回**（`labels_added`）、末位没有残留训练（`final_still_trained`）。
5. **统计层面**：每个 `.csv.gz` 行都是合法 `start,end`；OUT 的 `dataset_statistics.json` 能通过训练启动检查的读法——`row_aligned_parts` 为 true、`total_instances` 等于元数据行数、`trainable_tokens` 等于按 OUT 掩码实数的训练 token 数。

另加：每个 `--chat_dir`（与回放并列训练、修复不触碰的聊天缓存）行间铺满各 part 且**每行以 im_start 开头**——证明 H270 的规则在聊天缓存上本来也不会清任何东西；超过 `--max_seq_length` 的清单文档被计数（训练会截断它们，被清的 label 到不了任何 batch）。

PASS 条件与退出码：所有零计数器为 0、`found == pins["rows"]`、`dropped_at_final + already_untrained_at_final == pins["rows"]`，见 [scripts/data/check_replay_eos_mask.py:260-264](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L260-264)（组装 PASS 布尔：全部零计数器为零且两个行数等式成立）；`main` 据此返回 0 或 1，并可把完整结果（含 per-part 计数）写进 `--out` JSON，见 [scripts/data/check_replay_eos_mask.py:309-313](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L309-L313)（按需写 `--out` 文件、打印摘要、返回退出码）。

#### 4.4.3 源码精读

**零计数器**：[scripts/data/check_replay_eos_mask.py:57-78](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L57-L78)（定义 20 个必须为零的计数器名：从清单/回执失配、part 集合失配、文件不同、行不铺满、csv 与清单不同、畸形行，到统计缺失/不符、找不到文档、mask 非二值、错加、错清、末位残留、清单里出现 im_start 开头的文档、聊天行不以 im_start 开头）。注意两个「反向」计数器：`inventory_starting_im_start` 要求回放清单里**没有**聊天行混入，`chat_not_im_start` 要求聊天缓存里**所有**行都以 im_start 开头——两侧共同确认「回放 vs 聊天」的划分是干净的。

**钉子与文件枚举**：[scripts/data/check_replay_eos_mask.py:148-164](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L148-L164)（算 manifest 的 sha256 与钉子比对；读出清单后核对 id 列表 sha、行数、token 总数、id 重复；随后对 SRC 与 OUT 各跑一遍 `list_parts`，要求无孤儿文件且三类文件的索引集合等于清单的 part 集合）。`list_parts` 与 `csv_rows` 的实现见 [scripts/data/check_replay_eos_mask.py:89-125](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L89-L125)（枚举 part 文件并识别孤儿；把 `.csv.gz` 解析成 `(start,end)` 行、数出畸形行数；docstring 注明 OLMo-core 解析不了畸形行而启动检查会把它数成一行）。

**逐 part 的清单核对**：[scripts/data/check_replay_eos_mask.py:178-198](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L178-L198)（以 mask 文件字节数为 token 数、按 token 字节数推出 uint 宽度打开 memmap；验证清单行铺满 part 且 `.csv.gz` 行与清单相等；然后逐文档验证「长度 == length_with_eos、末 token == EOS、去尾哈希 == tokens_sha256」，找到计 `found`、否则计 `not_found`）。注意它的 dtype 推导方式与 writer 不同但殊途同归——`token_memmap` 见 [scripts/data/check_replay_eos_mask.py:109-111](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L109-L111)（按「token 文件字节数 ÷ mask 文件字节数」决定 uint 宽度打开只读 memmap）。

**掩码差分**——本脚本的灵魂：

```python
src, out = src != 0, out != 0
final = ends - 1
cleared = src & ~out
cleared[final] = False
part.update({
    "dropped_at_final": ..., "already_untrained_at_final": ...,
    "final_still_trained": int(out[final].sum()),
    "cleared_elsewhere": int(cleared.sum()),
    "labels_added": int((~src & out).sum()),
})
```

见 [scripts/data/check_replay_eos_mask.py:199-218](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L199-L218)（把两个 mask 读成布尔后做集合运算：末位上「SRC 训练 → OUT 不训练」计为 dropped，「SRC 本就不训练」计为 already untrained；末位之外的任何清零计 `cleared_elsewhere`，任何从 0 变 1 计 `labels_added`；同时验证 mask 是二值的）。三个方向各抓一种 writer 错误：漏清（`final_still_trained`）、多清（`cleared_elsewhere`）、错加（`labels_added`）。

**统计核对**：[scripts/data/check_replay_eos_mask.py:241-258](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L241-L258)（按训练启动检查的读法核对 OUT 的 `dataset_statistics.json`：`row_aligned_parts` 必须为 true、`total_instances` 等于 OUT 所有 `.csv.gz` 的行数、`trainable_tokens` 等于按 OUT 掩码实数出来的训练 token 总数）。

#### 4.4.4 代码实践

**实践目标**：把「每类错误 → 哪个计数器」的映射背下来，并理解测试如何先造正确目录再逐类破坏。

**操作步骤**：

1. 阅读 [tests/test_check_replay_eos_mask.py:64-77](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L64-L77)（`setup_dirs` / `run`：先用 writer 造出正确目录，再用目录自身的清单算出 pins 喂给 `check`——测试里钉子是现场推导的，真实运行时钉子来自构建回执）。
2. 填写下面这张映射表（左列已给出，补全中列与右列）：

| 测试用例 | 破坏方式 | 应触发的计数器 |
| --- | --- | --- |
| [test_passes_the_written_dir](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L87-L92)（不破坏） | 无 | pass；`found == dropped_at_final == 3`、`chat_docs == 2`、`inventory_over_max_seq_length == 1` |
| [test_an_already_untrained_final_label_is_valid](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L97-L101) | 把 SRC 某文档的末位 label 改成 False 再跑 writer | pass；`dropped_at_final=2`、`already_untrained_at_final=1` |
| [test_fails_a_label_cleared_elsewhere](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L104-L108) | 清掉 OUT 里内部 EOS 的 label | `cleared_elsewhere == 1` |
| [test_fails_a_replay_final_eos_left_trained](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L111-L115) | 把 OUT 某末位 label 设回 True | `final_still_trained == 1` |
| [test_fails_an_unmasked_copy](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L118-L121) | 拿 SRC 冒充 OUT | `final_still_trained == 3` |
| [test_fails_a_document_that_does_not_match_the_inventory](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L124-L131) | 改 SRC 的 token（OUT 的 symlink 连带变化） | `not_found == 1` |
| [test_fails_a_manifest_that_does_not_match_its_pins](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L134-L137) | 把 pins 的 `manifest_sha256` 改错 | `manifest_sha256_mismatch` |
| [test_fails_orphan_and_missing_parts](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L140-L150) | 加孤儿 part / 删 part / 加 `token_ids_part_backup.npy` | `part_set_mismatch` / `orphan_part_files` |
| [test_fails_a_chat_cache_row_without_im_start](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L153-L158) | 聊天目录混入不以 im_start 开头的行 | `chat_not_im_start == 1` |
| [test_fails_a_blank_metadata_line](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L161-L167) | 给 `.csv.gz` 追加空行（symlink 连带） | `metadata_malformed_lines == statistics_mismatch == 1` |
| [test_fails_missing_output_statistics](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L170-L174) | 删掉 OUT 的 `dataset_statistics.json` | `statistics_missing == 1` |
| [test_fails_a_chat_part_whose_rows_do_not_tile_it](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L177-L184) | 聊天目录的 csv 行不铺满 | `untiled_parts == 1` |
| [test_fails_an_incomplete_or_empty_chat_dir](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/tests/test_check_replay_eos_mask.py#L187-L195) | 删聊天 mask part / 传空目录 | `part_set_mismatch == 1` |

3. 可选验证（**待本地验证**）：`uv run pytest tests/test_check_replay_eos_mask.py -v`。本环境未执行，不要假设其输出。

**需要观察的现象**：`test_fails_a_blank_metadata_line` 里**一个**空行同时触发**两个**计数器——畸形行本身，以及 `trainable_tokens` 对账失败（统计里的行数与元数据行数不再相等）。

**预期结果**：每条拒绝路径都有唯一的计数器指纹；「正确目录」的两个正向用例分别验证「全清」与「部分本来就没训练」两种合法形态。

#### 4.4.5 小练习与答案

**练习 1**：pins 里为什么除了 `manifest_sha256` 还要有 `ids_sha256`、`rows`、`tokens` 三个数？它们来自哪里？

答案：来自构建的 R2 回执（ledger 的 `experiments/h182/receipts/r2_b1c12r.json`），是**独立于 manifest 的第二个来源**。若只有 manifest sha，重写 manifest 的人只要顺带更新 checker 常量就能骗过去；行数、token 总数与 id 列表哈希来自回执，与 manifest 的内容互锁——manifest 内容变了，即便 sha 常量被同步改掉，`rows_mismatch` / `tokens_mismatch` / `ids_sha256_mismatch` 也会响。多来源互锁是这类数据审计的通用手法。

**练习 2**：为什么核对脚本连聊天缓存也要管？它明明不在修复范围内。

答案：因为修复的**安全性**主张之一是「不误伤聊天行」。checker 对每个 `--chat_dir` 验证行间铺满且全部以 im_start 开头——这正是「H270 的规则若应用在聊天缓存上什么也不会清」的独立佐证。修复的验证从来不只是「该做的做了」，还包括「不该动的没动」。

**练习 3**：把 4.2.4 里你的三文档手工例子（含聊天行 A 和非 EOS 结尾的文档 B）喂给这个 checker 会怎样？

答案：会 FAIL，而且不是 checker 有 bug。它的清单模型假设回放目录的 inventory **全部**是「以 EOS 结尾的回放文档」：`found` 要求每个文档末 token 是 EOS（文档 B 不满足 → `not_found`），`final_still_trained` 要求每个清单文档的末位 label 在 OUT 中未训练（聊天行 A 的末位 EOS 是**要**训练的 → 非零）。也就是说：writer 的规则是宽容的（容忍混入的聊天行与非 EOS 结尾，只把数量记进 counts），而 checker 的钉子模型是为 H182 那种「纯回放、全 EOS 结尾」的构建准备的。两边约定不同，真实数据（纯回放）下并不冲突——这也是「pins 来自构建回执」的深层含义：checker 检查的是**那次具体构建**的清单，不是抽象的任意目录。

### 4.5 打包不变性与统计重算

#### 4.5.1 概念说明

修复方案有一条贯穿始终的工程原则：**只动监督，不动数据**。OUT 与 SRC 相比：

- token part、`.csv.gz` part、manifest：**symlink 原文件**——逐字节相同；
- mask part：每个 part 复制后只在选中位置清零；
- `dataset_statistics.json`：**全新重算**。

于是训练侧的一切「形状」都不变：OLMo-core 的打包（`doc_lens`）、实例划分、attention/位置重置、数据顺序、随机洗牌的种子轨道全部照旧；变的只有每个回放文档末位 EOS 的那一个 loss 位。这被称为**打包不变性**。它把「换数据」的风险面压缩到极限——训练命令唯一要改的就是 `--extra_numpy_dirs` 从 SRC 换成 OUT。

而统计**必须**重算，有两层原因。第一，`trainable_tokens` 变了：它必须按新 mask 实数。第二，也是最容易被忽视的：源回放目录是 c11 构建写的，它**没有** `dataset_statistics.json`（只有 `stats.json`）——按训练侧规则，一个多 part 目录在元数据边界模式下必须携带 `row_aligned_parts: true` 标记才能通过启动校验（见 2 节的 `_check_row_aligned_cache`）。writer 写出的统计把这个标记置 true（清单行铺满 part、每文档恰在一个 part 内，校验时已证），顺带让 OUT 比 SRC **更容易**被训练接受。

#### 4.5.2 核心流程

写阶段的尾部三件事（对照 4.3.2 的流程图末端）：

```
每个 part: copy(src_mask) → memmap r+ → mask[positions] = 0 → flush
          symlink(token part)、symlink(.csv.gz part)
symlink(manifest.jsonl)
写 dataset_statistics.json:
    configuration.row_aligned_parts = True
    configuration.replay_eos_mask = {source, rule, eos/im_start id,
                                     documents, starting_im_start, not_ending_eos,
                                     dropped, already_untrained, parts[每 part 的 selected/dropped]}
    overall_statistics = {total_instances=文档数, total_tokens,
                          trainable_tokens=按新 mask 实数, trainable_percentage}
```

之后 CLI 立即跑训练侧启动检查并对账：`_check_extra_numpy_dir` 数出的 `num_loss_tokens` 必须等于统计里的 `trainable_tokens`。

#### 4.5.3 源码精读

- 写 mask 与 symlink：[open_instruct/numpy_dataset_conversion.py:805-821](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L805-L821)（`os.makedirs` 后逐 part：复制源 mask、`r+` memmap 在 plan 位置写 0 并 flush、按 `was_trained` 累计 dropped 与 already_untrained、symlink token 与 `.csv.gz`、最后 symlink manifest）。
- 新统计的组装：[open_instruct/numpy_dataset_conversion.py:822-846](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L822-L846)（构造 `dataset_statistics.json`：`row_aligned_parts` 置 True；`replay_eos_mask` 变更块记录来源、规则文本、两个 token id、三类透明度计数、dropped/already_untrained 与每 part 的 selected/dropped；`overall_statistics` 的 `trainable_tokens` 用 `np.count_nonzero` 按新 mask 实数）。
- 启动检查与对账：[scripts/data/mask_replay_final_eos.py:43-51](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/mask_replay_final_eos.py#L43-L51)（用 dolma2 分词器配置与 OLMo-core 的 dtype 规则跑 `_check_extra_numpy_dir`——正是训练任务启动时对每个 `--extra_numpy_dirs` 条目跑的那个函数——再跑 `_check_row_aligned_cache`，若 `num_loss_tokens` 与统计的 `trainable_tokens` 不等则抛 ValueError）。`_check_extra_numpy_dir` 对多 part 元数据边界目录强制要求行对齐统计，见 [open_instruct/olmo_core_finetune.py:243-251](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/olmo_core_finetune.py#L243-L251)（多 part 目录必须带 `dataset_statistics.json` 且通过行对齐校验，否则启动被拒）。

打包不变性的端到端证明在 [open_instruct/test_numpy_dataset_conversion.py:1051-1085](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L1051-L1085)（`test_packed_training_drops_only_whole_replay_final_eos`：对 SRC 与 OUT 各构建一个 OLMo-core 打包数据集，断言实例数、`input_ids`、`doc_lens` 逐位相同；`label_mask` 只减不增；每个减少位置向前看两个 token 恰为「文档倒数第二个 token + EOS」）。注意它造了一个超过 `sequence_length=8` 的文档：截断会把末 EOS 切掉，所以被清的 label 根本进不了 batch，`dropped` 统计是 4（writer 按清单选了 4 个），而打包层面真正少掉的 loss 恰好是 3 个「装得下」的文档——docstring 里的 `too_long's is cut by packing` 注释说的就是这件事。writer 的 docstring 也如实声明了这个边界，见 [open_instruct/numpy_dataset_conversion.py:755-757](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/numpy_dataset_conversion.py#L755-L757)（说明 OLMo-core 按整文档打包并截断超长文档，截断文档的末 EOS 连同被清的 label 一起丢失，对其不做任何清除）。

「源目录连启动检查都过不了、OUT 能过」的直接证据：[open_instruct/test_numpy_dataset_conversion.py:967-979](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L967-L979)（`test_masked_dir_passes_startup_validation_on_main`：c11 布局的多 part 源目录因没有 `dataset_statistics.json` 被启动检查拒绝；掩码后的 OUT 通过同一检查，且 loss token 数与统计一致、`row_aligned_parts` 标记为 True）。

#### 4.5.4 代码实践

**实践目标**：验证「换目录即换数据」的最小改动主张，并理解统计重算的两层原因。

**操作步骤**：

1. 读 [open_instruct/test_numpy_dataset_conversion.py:936-965](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L936-L965) 中的三个断言组：(a) `_file_bytes(src)` 在 writer 运行前后相等；(b) `os.path.realpath(os.path.join(out, name))` 指回 SRC 的 token/csv/manifest；(c) `trainable_tokens` 的算术——23 个 token，减去 1 个本来就没训练的、4 个聊天 prompt token、3 个被清的，等于 15。
2. 读 [open_instruct/test_numpy_dataset_conversion.py:981-990](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/open_instruct/test_numpy_dataset_conversion.py#L981-L990)（`test_dir_whose_documents_all_start_with_im_start_is_byte_identical`：全是聊天行的目录经 writer 后 mask 与源逐字节相同、`dropped == 0`——规则的「零误伤」在字节层面的直接体现）。
3. 复算第 1 步 (c)：列出那 23 个 token 各自的 mask 归属（聊天 prompt 4 个 0、聊天内容与 EOS、回放文档内容、已被清的 3 个位置），验证 15 这个数。

**需要观察的现象**：symlink 让「token 不变」成为一个文件系统层面可检查的事实，而不是代码里的承诺；统计中的 `trainable_tokens` 与启动检查实数的 `num_loss_tokens` 是**两套独立计数**在对账。

**预期结果**：纸面复算与断言一致；若你在本地跑该测试（**待本地验证**），两条数字路径也应一致。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `row_aligned_parts` 必须由 writer 亲自写 true，而不能指望训练侧通融？

答案：训练侧 `_check_row_aligned_cache` 是硬门：多 part 目录在元数据边界模式下没有这个标记直接拒绝启动。源目录（c11 布局）根本没有这份文件。writer 在校验阶段已经证明了「清单行铺满 part、每个文档恰在一个 part 内」——这正是行对齐的定义，所以标记置 true 是**把已证明的事实写进统计**，而不是无根据的声明。

**练习 2**：如果 writer 偷懒沿用源的 `stats.json`、只是把 mask 换掉，会在哪一步穿帮？

答案：第一个穿帮点是训练启动——多 part 目录没有 `dataset_statistics.json`（或没有行对齐标记）直接被拒。即便绕过它，`mask_replay_final_eos.py` 的 CLI 对账也会抛错：启动检查实数的 loss token 与统计不符（[scripts/data/mask_replay_final_eos.py:50-51](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/mask_replay_final_eos.py#L50-L51)，若启动检查的 loss token 计数与统计不符则抛 ValueError）；checker 的 `statistics_mismatch` 也会抓到。

**练习 3**：一个 90,000 token 的回放文档（`max_seq_length = 65536`）会被怎么处理？这算修复的漏洞吗？

答案：writer 仍会清它末位 EOS 的 label（按清单规则它入选）；但 OLMo-core 打包时把它截断到 65,536 个 token，末 EOS 连同被清的 label 一起被截掉——**没有任何 batch 包含那个位置**。checker 把这类文档数进 `inventory_over_max_seq_length`（[scripts/data/check_replay_eos_mask.py:211-212](https://github.com/allenai/open-instruct/blob/095d931f5d2f3ffe02a682d272c012f4feb21767/scripts/data/check_replay_eos_mask.py#L211-L212)，统计超长的清单文档数），docstring 说明「训练会截断它们，被清的 label 到不了 batch」。不算漏洞：截断文档本来就不携带它的末位 EOS 进训练，无可修复之处，写入端保持规则一致反而让「目录替换」不需要任何 per-case 判断。

## 5. 综合实践

把五个模块串成一个纸面演练（全部不需要 GPU；涉及运行的部分均**待本地验证**）。

**任务**：假设你接手一份新的长上下文回放目录，需要用本讲的链路把它修好并给出审计证据。

1. **画数据流图**（module 4.2 + 4.3）：对照 `write_replay_eos_masked_dir` 画出「SRC 的 token 分 part + manifest 文档边界 → 逐 part 校验 → 选中位置 → 写新 mask part + symlink 其余 → 新统计 → 启动检查对账」的完整数据流，标出每一步可能抛出的拒绝。
2. **手工推演**（module 4.2.4）：用两个 part、三个文档（聊天行、非 EOS 结尾的回放文档、EOS 结尾的回放文档）的例子算出：哪些位置被清、`dropped` / `already_untrained` 各是多少、OUT 里哪些文件是 symlink。
3. **映射测试**（module 4.3.4 + 4.4.4）：从两张映射表里各挑三条拒绝路径，写出「构造什么坏目录 → writer 或 checker 的哪个计数器/报错响」。
4. **本地验证**（可选）：`uv run pytest open_instruct/test_numpy_dataset_conversion.py -k Replay -v` 与 `uv run pytest tests/test_check_replay_eos_mask.py -v`，把每条用例的通过情况与你的纸面预测对齐。

**参考答案——两个设计问题**：

**问题 A：为什么清掉的是「预测该 EOS 的那个目标」，而不是把 EOS 这个位置从数据里拿掉？**

label mask 按**目标 token** 索引：`mask[i] = 1` 的含义是「第 i 个 token 作为预测目标参与 loss」。文档 `[start, end)` 末位 EOS 的目标项位于 `end - 1`——它表示「以同一文档前文为条件、预测出 EOS」。清掉 `mask[end-1]` 恰好删除且仅删除这条监督。其他候选位置都不对：清 `mask[end-2]` 删掉的是「预测文档倒数第二个 token」的内容监督；而「EOS 作为输入」一侧并不存在需要清理的目标——EOS 之后的 token 属于下一个文档，OLMo-core 的打包以 `doc_lens` 划分文档，跨文档的预测从不被训练（打包级测试断言每个被清的位置向前看两个 token 恰为 `doc[-2], EOS`，即「上一个 token 预测 EOS」那一项）。至于把 EOS token 从 token 流里删除：那会移动其后所有偏移、作废 manifest 与 `.csv.gz` 与全部内容哈希、破坏 EOS 扫描模式下的文档分隔、改变打包与数据顺序——即从「修监督」升级成「重写数据」。修复的最小干预原则就是：**token 一位不动，只翻监督位**。

**问题 B：为什么 writer 宁可拒绝也要把文档边界绑定到构建 manifest，而不做 EOS 扫描？**

三个理由层层递进。（1）**正确性**：回放文档是预训练式长文本，内部完全可能含 EOS；EOS 扫描会把内部 EOS 当边界，伪文档的「末位 EOS」被清掉的是合法内容监督——`test_refuses_csv_rows_that_differ_from_the_manifest` 的场景（行在内部 EOS 处切开）正是扫描式边界的样子，writer 对它报 `differ from the manifest`。（2）**可审计**：manifest 是构建时逐文档写下的 ground truth，sha256 钉子把它锁到那次具体构建；「训练读的边界（`.csv.gz`）== 构建写的边界（manifest）== writer 选位置用的边界」三方逐行相等，使每一步都可对外证明。（3）**代价不对称**：这些目录是长跑训练的输入，错误掩码是**静默**的数据损坏——训练照常跑、指标悄悄坏；而一次明确的拒绝只是几分钟的人工排查。fail-closed（宁拒不猜）因此是唯一合算的选择：writer 的全部校验都排在写任何字节之前，OUT 要么完整、要么不存在，`test_refuses_a_manifest_that_is_not_the_pinned_one` 里那份「manifest 与 csv 互相一致但都不是构建产物」的目录，正是靠钉子而不是靠互相校验才被识破的。

## 6. 本讲小结

- **H270 的问题**：回放文档以全 True 掩码进入 `--extra_numpy_dirs`，末位 EOS 成为目标，教会模型「超长上下文后停止」；修复的判定规则 = 长度 ≥ 2 ∧ 不以 im_start 开头 ∧ 以 EOS 结尾，清 `end-1` 一个 label 位。
- **两种 EOS 的分界**是首 token：聊天行以 `<|im_start|>`（100264）开头、其回合末 EOS 是**要保留**的停止监督；回放文档不以它开头、其末位 EOS（100257）是**要清除**的伪监督——单 token 文档、非 EOS 结尾文档、内部 EOS 全部豁免。
- **边界必须来自构建 manifest**：`replay_final_eos_positions` 是不做边界推断的纯函数；EOS 扫描或 part 截断都会把内部 EOS 误判成末位。sha256 钉子 + 四方索引集合一致 + 清单铺满 part + csv 行逐行相等，构成「fail-closed」的信任链，且所有校验先于任何写入。
- **打包不变性**：token/`.csv.gz`/manifest 用 symlink 原样保留，mask 每 part 复制后清位——打包、数据顺序、实例划分完全不变，训练命令只需把 SRC 换成 OUT；超长文档被截断，其被清的 label 到不了 batch。
- **统计必须重算**：`trainable_tokens` 按新 mask 实数、`row_aligned_parts: true` 把已证明的行对齐事实写进 `dataset_statistics.json`，使多 part 的 OUT 能通过训练启动检查（而 c11 布局的源目录过不了），CLI 还做了统计与启动检查的 loss token 对账。
- **checker 是机制检查**：独立实现（numpy + 标准库）、期望来自构建回执的四个钉子而非被测规则、对真实数据做全量差分（漏清/多清/错加三个方向各有计数器）、退出码 1 表示任何差异——它证明数据变换正确，不证明模型行为变化（那是评测的事）。

## 7. 下一步学习建议

- **回到训练侧**：u7-l1（`olmo_core_finetune.py`）精读训练如何消费这些目录——`_check_extra_numpy_dir` / `_check_row_aligned_cache` 的完整逻辑、`_document_boundary_kwargs` 在「元数据边界」与「EOS 扫描」两种模式间的取舍，本讲只借用了它们的片段。
- **同类问题的另一端**：u7-l4（MoE checkpoint 导出）与 u7-l6（TPU MaxText SFT）处理的是「导出必须存训练 forward 用过的值」这一精度层面的不变性问题；本讲处理的是「目录必须携带构建写下的边界」这一数据层面的不变性问题——两讲合读能看到同一套「绑定构建事实、拒绝猜测」的工程思想在不同层的再现。
- **继续读测试**：`open_instruct/test_numpy_dataset_conversion.py` 的 `TestRowAlignedParts`（L415 起）解释了 `row_aligned_parts` 标记的来历（#1940 的行对齐 opt-in），是理解本讲「为什么源目录过不了启动检查」的前史。
- **动手方向**：为一份自己构造的玩具回放目录（照 `_write_replay_dir` 的方式）完整跑一遍 `mask_replay_final_eos.py` 与 `check_replay_eos_mask.py`（配 `--pins_json`），把 checker 的 JSON 输出逐字段解读一遍——这是检验你是否真的理解 20 个零计数器含义的最快方式（**待本地验证**）。
