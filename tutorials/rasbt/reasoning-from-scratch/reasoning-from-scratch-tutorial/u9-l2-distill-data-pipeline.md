# 蒸馏数据管线：从 JSON 到训练样本

## 1. 本讲目标

上一讲（u9-l1）我们用教师模型（DeepSeek-R1 或 Qwen3-235B）生成了带思考流的 JSON 数据。本讲要解决的问题是：**这些 JSON 如何变成可以直接喂给损失函数的训练样本？**

学完本讲，你应该能够：

1. 使用 `load_distill_data` 加载四个分区的蒸馏数据集，并解释它的「本地缓存优先 + 主备双 URL」策略。
2. 说出 `format_distilled_answer` 如何把教师的「思考流 + 正式回答」双字段包装成 `<think>…</think>` 形式的单一监督目标。
3. 逐行理解 `build_examples` 的三步流程：编码 prompt（套聊天模板）→ 编码 answer（裸编码）→ 拼接 EOS，并解释为什么每个样本要记录 `prompt_len`。
4. 使用 `compute_length` 与 `filter_examples_by_max_len` 做长度体检与超长样本过滤，并理解过滤在训练成本与模型上下文上限两个层面的必要性。

本讲全程不需要 GPU，也不加载模型——纯数据处理。

## 2. 前置知识

### 2.1 硬蒸馏：本仓库的路线

第 8 章开篇区分了三种蒸馏（见 [ch08/01_main-chapter-code/ch08_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/01_main-chapter-code/ch08_main.ipynb) 8.1 节）：

- **硬蒸馏（hard distillation）**：学生模型把教师生成的**文本**当作标准答案，做普通的下一词预测训练。损失就是交叉熵 \(\mathcal{L} = \text{CE}(y_{\text{teacher}}, y_{\text{student}})\)。本质上是在合成数据上做监督微调（SFT）。
- **软蒸馏（soft distillation）**：学生匹配教师在全词表上的**概率分布**，最小化 KL 散度。需要教师的 logits。
- **混合式**：两者加权组合。

本仓库（以及 DeepSeek-R1 对小模型的做法）选的是**硬蒸馏**：闭源 API 不给 logits、师生词表也未必对齐，纯文本最通用。这也意味着——数据管线的全部工作就是**文本整理 + 分词**，本讲的三个函数正由此而来。

### 2.2 为什么「预先编码、反复复用」

第 6、7 章的 RLVR 训练中，每条题目在一个 epoch 里只被采样一次（rollout 是随机生成的，每次都不同），所以 prompt 的编码可以在训练循环里**现做现用**。而蒸馏是 SFT：**同一条样本要被看很多遍（多个 epoch）**。既然编码结果是确定性的（同一段文本永远编出同一串 token id），就没有理由每个 epoch 重复编码。所以本章的管线是**离线一次性**的：

```
JSON 数据 → 编码一次 → [{token_ids: [...], prompt_len: n}, ...] → 反复喂给训练循环
```

### 2.3 训练样本长什么样，prompt_len 有什么用

每条训练样本是一个只有两个键的字典：

```python
{"token_ids": [1, 35, 987, ...],   # prompt + answer + eos 的完整 token 序列
 "prompt_len": 47}                  # 前 47 个 token 属于 prompt
```

`prompt_len` 是给下一讲（u9-l3）的蒸馏损失准备的：损失**只对答案区间计算**，不算 prompt 部分。预告一下公式——设序列总长为 \(T\)、prompt 长度为 \(p\)，下一讲的损失是

\[
\mathcal{L} = -\frac{1}{T-p}\sum_{t=p-1}^{T-2} \log p_\theta\left(x_{t+1} \mid x_{\le t}\right)
\]

即从「最后一个 prompt token 预测第一个 answer token」的位置开始算交叉熵，共 \(T-p\) 个预测目标（answer + eos）。本讲的任务就是保证 `token_ids` 与 `prompt_len` **精确对齐**。

### 2.4 承接前讲

- **u9-l1**：数据四字段契约 `problem` / `gtruth_answer` / `message_thinking` / `message_content`。注意 `gtruth_answer`（标准答案）**不进训练**，只用于评估；教师输出才是监督目标。
- **u2-l6**：`Qwen3Tokenizer` 的四个开关（`apply_chat_template`、`chat_wrapped`、`add_generation_prompt`、`add_thinking`）以及按文件名判定 eos 的规则。
- **u4-l1**：`render_prompt` 的 `\boxed{ANSWER}` 输出契约。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| [reasoning_from_scratch/ch08.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L22-L167) | L22-167 | 本讲主角：五个数据处理函数 |
| ↳ L22-74 | `load_distill_data` | 下载/缓存蒸馏数据集（四分区） |
| ↳ L77-83 | `format_distilled_answer` | 教师双流 → `<think>` 包装 |
| ↳ L86-99 | `load_reasoning_tokenizer` | 下载并配置 reasoning 分词器 |
| ↳ L102-134 | `build_examples` | 编码 + 拼接，产出训练样本 |
| ↳ L137-154 | `compute_length` | 长度统计 |
| ↳ L157-167 | `filter_examples_by_max_len` | 超长样本过滤 |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L413-L420) | L413-420 | `render_prompt`：复用第 3 章的 `\boxed{}` 提示模板 |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L347-L419) | L347-419 | `Qwen3Tokenizer`：encode 的 `chat_wrapped` 分支与 eos 规则 |
| [ch08/01_main-chapter-code/ch08_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/01_main-chapter-code/ch08_main.ipynb) | 8.3-8.4 节 | 章节主 notebook 的逐步演示（本讲引用其实测数字） |
| [tests/test_ch08.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch08.py#L66-L257) | L66-257 | 对应单元测试，实践的权威依据 |

另外，[ch08.py L15-L19](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L15-L19) 的 import 块就是本管线唯一的两处外部依赖：`from .ch03 import render_prompt` 与 `from .qwen3 import Qwen3Tokenizer, download_qwen3_small`——模板复用第 3 章、分词复用第 2 章，零新增基础设施。

## 4. 核心概念与源码讲解

### 4.1 load_distill_data：四分区蒸馏数据的下载与缓存

#### 4.1.1 概念说明

u9-l1 讲过，蒸馏数据集由「教师模型 × 题目集」组合而成，作者把四种组合都发布在了 Hugging Face 数据集 [`rasbt/math_distill`](https://huggingface.co/datasets/rasbt/math_distill) 上：

| 分区名 | 教师 | 题目集 | 规模 |
| --- | --- | --- | --- |
| `deepseek-r1-math-train` | DeepSeek-R1 | 12000 题 MATH 训练集 | ~105 MB（本讲主线） |
| `deepseek-r1-math500` | DeepSeek-R1 | MATH-500 测试集 | 500 条（轻量替代） |
| `qwen3-235b-a22b-math-train` | Qwen3-235B | 12000 题训练集 | — |
| `qwen3-235b-a22b-math500` | Qwen3-235B | MATH-500 | — |

`load_distill_data` 要解决三个问题：确定本地缓存路径、从远端拉数据（主 URL 失败换备用）、落盘以便下次离线使用。这个模式和你在 u7-l1 见过的 `load_math_train`（第 6 章 RLVR 训练数据）是同一套「本地优先、主备双源」设计。

#### 4.1.2 核心流程

```
输入: local_path=None, partition="deepseek-r1-math-train", save_copy=True

1. local_path 为空 → 默认取 f"{partition}.json"
2. 若本地文件存在 → 直接 json.load 返回，打印 "(cached)"，全程不联网
3. 校验 partition 是否属于四个白名单分区（assert）
4. 请求主 URL（Hugging Face datasets）→ 失败则请求备用 URL（Backblaze B2）
5. r.json() 解析 → save_copy=True 时以 indent=2 写盘
6. 返回 list[dict]
```

注意一个**顺序细节**：第 2 步（缓存检查）在第 3 步（assert 白名单）**之前**。也就是说，只要本地文件存在，传入任何 `partition` 字符串都会直接读本地文件——「本地文件即真相」。

#### 4.1.3 源码精读

函数签名与默认路径——分区名直接充当缓存文件名，天然避免不同分区互相覆盖：

- [reasoning_from_scratch/ch08.py:L22-L30](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L22-L30)：`load_distill_data` 定义三个参数；`local_path` 缺省时拼成 `{partition}.json` 并转成 `Path` 对象。

主备两个下载源，与全书其他下载器一致的「HF 主、B2 备」双通道：

- [reasoning_from_scratch/ch08.py:L32-L40](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L32-L40)：主 URL 指向 `huggingface.co/datasets/rasbt/math_distill/resolve/main/data/{partition}.json`，备用 URL 指向 Backblaze B2 桶 `reasoning-from-scratch/MATH/{partition}.json`。

缓存命中分支——先于白名单校验执行，命中时打印文件大小与 `(cached)` 标记后立即返回：

- [reasoning_from_scratch/ch08.py:L42-L48](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L42-L48)：`local_path.exists()` 为真则打开、`json.load`、打印 KB 大小并 `return data`。

白名单断言——只允许四个官方分区，防止拼错分区名后拼出 404 URL：

- [reasoning_from_scratch/ch08.py:L50-L55](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L50-L55)：`assert partition in (...)` 列出四个合法分区名。

下载与回退——主 URL 抛 `RequestException` 时打印提示并改请求备用 URL：

- [reasoning_from_scratch/ch08.py:L57-L63](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L57-L63)：`try: requests.get(url, timeout=30)` → `except requests.RequestException:` → `requests.get(backup_url, timeout=30)`。

解析与落盘——**注意 `json.dump` 在下载完成、`r.json()` 解析成功之后才执行**，所以下载中途断网不会留下半截缓存文件（也没有断点续传；下次重跑会重新下整个文件）：

- [reasoning_from_scratch/ch08.py:L65-L74](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L65-L74)：`data = r.json()`，`save_copy=True` 时以 `indent=2` 写盘并打印大小，最后返回 `data`。

与 u1-l3 精读过的 `utils.download_file` 相比，这个函数**刻意更简单**：没有流式分块下载（`requests.get` 一次性把 ~105 MB 拉进内存）、没有「本地大小 vs Content-Length」校验、没有结构化报错。代价是内存峰值高、无断点续传；收益是十几行搞定一个一次性下载。工程上「够用就好」的取舍样本。

notebook 的实测数字（[ch08_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/01_main-chapter-code/ch08_main.ipynb) 8.3 节输出）：`deepseek-r1-math-train.json: 107538.0 KB (cached)`，`Dataset size: 12000`。单元测试也验证了缓存路径完全不联网——[tests/test_ch08.py:L66-L82](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch08.py#L66-L82) 把 `requests.get` monkeypatch 成「一被调用就断言失败」，缓存命中时它确实没被调用。

#### 4.1.4 代码实践

**实践目标**：验证下载-缓存行为，并用轻量分区先跑通流程。

**操作步骤**：

```python
# 示例代码：practice_load_data.py
from reasoning_from_scratch.ch08 import load_distill_data

# 第一步：先用 500 条的轻量分区（几 MB，30 秒内）
math500 = load_distill_data(partition="deepseek-r1-math500")
print("math500 size:", len(math500))
print("fields:", sorted(math500[0].keys()))

# 第二步：再跑 12000 条主线分区（首次约 105 MB，请耐心）
math_train = load_distill_data(partition="deepseek-r1-math-train")
print("math_train size:", len(math_train))

# 第三步：把上面两行再执行一遍，观察输出变化
```

**需要观察的现象**：

1. 每个分区第一次运行时会打印 `xxx.json: <大小> KB`；第二次运行打印 `xxx.json: <大小> KB (cached)`，且几乎瞬时返回。
2. 当前目录出现 `deepseek-r1-math500.json` 与 `deepseek-r1-math-train.json` 两个缓存文件。
3. math500 只有 500 条（有真实下载测试佐证，见 [tests/test_ch08.py:L114-L124](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch08.py#L114-L124) 断言 `len(returned) == 500`）。

**预期结果**：math500 分区 500 条、math-train 分区 12000 条；重复运行命中缓存不再联网。若网络受限，仅完成 math500 分区即可继续后续实践（把后文的 `math_train` 换成 `math500`，样本量小、结论同理）。

#### 4.1.5 小练习与答案

**练习 1**：缓存检查为什么放在 `assert partition in (...)` 之前？这种顺序有什么好处和隐患？

**答案**：好处是「本地文件即真相」——文件已在硬盘时完全不依赖网络与分区名校验，离线可复现，CI 里也能用预备好的缓存跑测试。隐患是分区名拼错时（例如 `deepseek-r1-math-trani`），只要恰好存在同名本地文件就不会报错，拼写错误被静默吞掉。

**练习 2**：对比 `utils.py` 的 `download_file`（u1-l3），这个函数缺少哪三样机制？为什么这里可以接受？

**答案**：缺流式分块下载（整文件进内存）、缺 Content-Length 大小校验（残缺文件不会被发现重下）、缺断点续传。可接受的原因：JSON 数据体量远小于模型权重（百 MB 级 vs GB 级）、只需下载一次、失败重下整个文件的代价小。

**练习 3**：如果下载进行到一半按下 Ctrl-C，本地会留下缓存文件吗？下次运行会续传吗？

**答案**：不会留下文件——`json.dump` 只在 `r.json()` 完整解析之后执行；也不会续传——下次运行发现文件不存在就从头下载整份。规格里说的「中断后用已缓存文件」指的是**完整下载落盘之后**，后续处理阶段（如 `build_examples`）可以随意中断重跑，加载步骤会秒读缓存。

### 4.2 format_distilled_answer 与 build_examples：从教师双流到训练样本

#### 4.2.1 概念说明

这是本讲的核心模块，回答三个问题：

**问题一：为什么要把两条消息流拼成一个字符串？** u9-l1 讲过，推理模型的输出是双流的：`message_thinking`（思考流）和 `message_content`（正式回答）。但学生的训练目标是**一整段连续文本**——模型生成时并没有「两条通道」，只有一个 token 接一个 token 的序列。所以必须决定两段文本在序列里的**排版**。本书的答案是 `<think>{thinking}</think>\n\n{content}`：用一对标签把思考流围起来，再空一行接正式回答。这与 Qwen3 官方推理模型的输出习惯一致，而且标签让下游工具（聊天界面、答案提取器）能可靠地把思考流剥掉或隐藏——第 7 章的 `reward_format`（u8-l4）检查的正是这对标签。

**问题二：prompt 和 answer 为什么用两种编码方式？** 训练序列 = prompt 部分 + answer 部分。prompt 走完整聊天模板包装（`<|im_start|>user … <|im_end|>\n<|im_start|>assistant`），answer 则**裸编码**（`chat_wrapped=False`）。如果 answer 也套模板，序列里会多出一整套 `<|im_start|>user` 开头——训练文本就不再是「一段对话」，学生学到的格式也是错的。

**问题三：为什么手动追加 eos？** `Qwen3Tokenizer.encode` 不会自动在末尾加结束符。蒸馏训练必须教学生「回答完了要停」，所以显式拼接 `[tokenizer.eos_token_id]`。reasoning 分词器的 eos 按 u2-l6 的文件名规则（[reasoning_from_scratch/qwen3.py:L381-L387](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L381-L387)：文件名含 `base` 且不含 `reasoning` 才用 `<|endoftext|>`，否则用 `<|im_end|>`）解析为 `<|im_end|>`。

另外还有一个**容错设计**：12000 条真实数据里难免有缺字段、空回答的坏行。`build_examples` 不让一条坏数据炸掉整趟构建，而是逐条 `try/except`，把坏行计入 `skipped` 返回给调用者。

#### 4.2.2 核心流程

`build_examples` 对每条数据执行三步，任何一步抛出 `KeyError`（缺字段）、`TypeError`、`ValueError`（如空 content）就跳过并计数：

```
对 data 中每条 entry:
    try:
        1. prompt 编码（套聊天模板）
           prompt = render_prompt(entry["problem"])     # \boxed{} 契约模板
           prompt_ids = tokenizer.encode(prompt)         # chat_wrapped=None → 回落 True

        2. answer 编码（裸编码）
           target = "<think>{thinking}</think>\n\n{content}"
           answer_ids = tokenizer.encode(target, chat_wrapped=False)

        3. 拼接
           token_ids = prompt_ids + answer_ids + [eos_token_id]
           长度 < 2 → 跳过
           记录 {"token_ids": token_ids, "prompt_len": len(prompt_ids)}
    except (KeyError, TypeError, ValueError):
        skipped += 1
```

最终一条样本解码回文本后的形态（示意）：

```
<|im_start|>user
You are a helpful math assistant.
Answer the question and write the final result on a new line as:
\boxed{ANSWER}

Question:
{题目}

Answer:<|im_end|>
<|im_start|>assistant
<think>{教师思考流}</think>

{教师正式回答，含 \boxed{...}}<|im_end|>
└──────────── prompt（prompt_len 个 token）────────────┘└──────── answer + eos ────────┘
```

#### 4.2.3 源码精读

**format_distilled_answer**——两行核心逻辑，严格来说只做「字符串拼接 + 护栏」：

- [reasoning_from_scratch/ch08.py:L77-L83](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L77-L83)：`str()` 把两个字段强转成字符串（防上游给出数字、None 等类型），`.strip()` 去首尾空白；`message_content` 为空直接抛 `ValueError`（没有正式回答的样本没有训练价值）；返回 `f"<think>{thinking}</think>\n\n{content}"`。单元测试 [tests/test_ch08.py:L127-L135](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch08.py#L127-L135) 断言的正是这个精确格式：`"<think>Reason carefully.</think>\n\nThe answer is 4."`。

**load_reasoning_tokenizer**——为管线配置分词器。注意四个开关全开：

- [reasoning_from_scratch/ch08.py:L86-L99](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L86-L99)：先 `download_qwen3_small(kind="reasoning", tokenizer_only=True)` 只下载分词器文件，再构造 `Qwen3Tokenizer(apply_chat_template=True, add_generation_prompt=True, add_thinking=True)`。`add_thinking=True` 的效果（[reasoning_from_scratch/qwen3.py:L411-L419](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L411-L419) 的 `_wrap_chat`）是在 `<|im_start|>assistant` 后只补一个换行、**不预填** `<think>`——思考标签留给学生自己写，这正是蒸馏想教的行为。

**encode 的 chat_wrapped 分支**——prompt 与 answer 两种编码方式的分岔点：

- [reasoning_from_scratch/qwen3.py:L389-L391](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L389-L391)：`encode(prompt, chat_wrapped=None)`——不传时回落到实例属性 `apply_chat_template`（本管线为 True），所以 prompt 自动套模板。
- [reasoning_from_scratch/qwen3.py:L397-L406](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L397-L406)：`chat_wrapped=True` 时先过 `_wrap_chat`；随后 `_SPLIT_RE` 把文本切成「特殊 token / 普通片段」交替序列，特殊 token 查表、其余走 BPE。注意 `<think>` 与 `</think>` **不在** `_SPECIALS` 表里（形状也不匹配 `<|...|>` 正则），它们作为普通词表 token 走 BPE——在 reasoning 词表中各自对应一个固定 id（第 7 章代码写死为 151667/151668，见 [reasoning_from_scratch/ch07.py:L13-L14](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L13-L14)）。

**build_examples**——三步编码与拼接的主体：

- [reasoning_from_scratch/ch08.py:L102-L110](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L102-L110)：Step 1 用第 3 章的 `render_prompt`（[reasoning_from_scratch/ch03.py:L413-L420](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L413-L420)）把 `problem` 包进「数学助手 + `\boxed{ANSWER}` 格式指令 + `Answer:` 起点」模板——评测、RLVR、蒸馏三条管线共用同一份 prompt 契约，保证训练与评测的输入分布一致。随后 `tokenizer.encode(prompt)` 得到带聊天包装的 prompt ids。
- [reasoning_from_scratch/ch08.py:L112-L121](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L112-L121)：Step 2 用 `format_distilled_answer` 产出目标文本并以 `chat_wrapped=False` 裸编码；Step 3 把三段拼成 `token_ids = prompt_ids + answer_ids + [tokenizer.eos_token_id]`——eos 是长度为 1 的列表，三者都是 `list[int]`，直接用 `+` 拼接。
- [reasoning_from_scratch/ch08.py:L123-L132](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L123-L132)：护栏与容错——总长不足 2 的序列跳过（下一讲要构造 `input_ids = token_ids[:-1]`、`target_ids = token_ids[1:]`，至少要有 1 个输入-目标对）；任何一条数据抛 `KeyError/TypeError/ValueError` 都只计入 `skipped`，不中断整体构建。
- [reasoning_from_scratch/ch08.py:L127-L130](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L127-L130)：样本字典只存 `token_ids` 与 `prompt_len` 两个键——原始文本、思考流、标准答案全部丢弃。这是「一次编码、反复复用」的最终形态：训练循环从此不再碰分词器。

对应测试 [tests/test_ch08.py:L196-L224](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch08.py#L196-L224) 用三条手工数据（一条正常、一条空 content、一条缺 `message_thinking`）验证：产出 1 条样本、跳过 2 条，且 `prompt_len` 等于 prompt 编码长度、`token_ids[-1] == tokenizer.eos_token_id`。

notebook 实测：对 12000 条全量数据，`build_examples` 产出 **12000 条样本、0 条跳过**——DeepSeek-R1 的生成质量足够好，没有触发任何坏行护栏。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：完整跑通「JSON → 训练样本」管线，并逐项核验拼接契约。

**操作步骤**：

```python
# 示例代码：practice_distill_pipeline.py
# 运行方式（仓库根目录）: uv run python practice_distill_pipeline.py
from reasoning_from_scratch.ch08 import (
    load_distill_data,
    load_reasoning_tokenizer,
    build_examples,
    compute_length,
    filter_examples_by_max_len,
)

# 1) 加载数据（首次下载较慢；若已完成 4.1 实践则命中缓存）
math_train = load_distill_data(partition="deepseek-r1-math-train")
print("Dataset size:", len(math_train))

# 2) reasoning 分词器（首次运行下载 tokenizer-reasoning.json 到 ./qwen3/）
tokenizer = load_reasoning_tokenizer()
print("eos_token:", tokenizer.eos_token, "| eos_token_id:", tokenizer.eos_token_id)

# 3) 构建样本。全量 12000 条编码需几分钟；调试阶段可先用子集：
#   examples, skipped = build_examples(math_train[:500], tokenizer)
examples, skipped = build_examples(math_train, tokenizer)
print("examples:", len(examples), "| skipped:", skipped)

# 4) 长度体检：总长 vs 仅答案区
compute_length(examples)                    # 全序列长度
compute_length(examples, answer_only=True)  # 总长 - prompt_len（答案 + eos）

# 5) 两种阈值的过滤对比
kept_2048 = filter_examples_by_max_len(examples, max_len=2048)
kept_1024 = filter_examples_by_max_len(examples, max_len=1024)

# 6) 抽查一条样本，核验拼接契约
ex = examples[0]
total = len(ex["token_ids"])
print(f"prompt_len={ex['prompt_len']}, total={total}, "
      f"answer_len={total - ex['prompt_len']}")
print("最后一个 token 是 eos 吗:", ex["token_ids"][-1] == tokenizer.eos_token_id)
print("prompt_ids 与 token_ids 前缀一致吗:",
      ex["token_ids"][:ex["prompt_len"]] == ex["token_ids"][:ex["prompt_len"]])
print("--- 整条样本解码回文本 ---")
print(tokenizer.decode(ex["token_ids"]))
```

**需要观察的现象**：

1. 第 2 步打印的 `eos_token` 应为 `<|im_end|>`（reasoning 分词器走 else 分支）。
2. 第 6 步两个核验都应为 `True`：末位 token 恰是 eos；解码文本中能看到 `<|im_start|>user … <|im_end|>\n<|im_start|>assistant\n<think>…</think>\n\n…` 的完整结构，且以 `<|im_end|>` 结尾。
3. `answer_only=True` 的平均长度明显小于总长平均（prompt 模板本身约占几十个 token）。

**预期结果**：全量运行时 `examples: 12000 | skipped: 0`；`max_len=2048` 过滤后剩 6695（notebook 实测）；`max_len=1024` 的过滤数量**待本地验证**——可以肯定显著少于 6695（阈值更严）。若下载受阻，用 `math500` 分区跑同管线（500 条），所有核验点同样成立。

#### 4.2.5 小练习与答案

**练习 1**：如果 answer 编码时忘传 `chat_wrapped=False`，会发生什么？

**答案**：`encode` 的 `chat_wrapped` 缺省为 `None`，会回落到实例属性 `apply_chat_template=True`（[qwen3.py:L389-L391](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L389-L391)），answer 会被再包一层 `<|im_start|>user\n…<|im_end|>\n<|im_start|>assistant\n`。训练序列里会出现两套对话框架，学生学到的「assistant 回答之后又冒出一个 user 消息」格式是错的，与推理时的输入分布不一致。

**练习 2**：`prompt_len` 记录的为什么必须是**编码后**的 prompt 长度，而不能在渲染文本时按字符数估算？

**答案**：下一讲的损失切片按 token 位置进行（`answer_start = prompt_len - 1`）。token 与字符没有稳定的换算关系（BPE 子词长度不一、还有 `<|im_start|>` 这类一串字符只占一个 id 的特殊 token），字符数估算会错位，导致损失算到 prompt 中段或漏掉答案开头。

**练习 3**：`token_ids` 的拼接顺序是 `prompt_ids + answer_ids + [eos]`。为什么 eos 必须在最后、且只有最后一个？

**答案**：eos 承担「教模型停止」的职责：训练时它是答案区最后一个预测目标，模型学会在回答完毕后输出 `<|im_end|>`，推理时的提前停止逻辑（u3-l1）依赖它触发。如果 eos 出现在中间，模型会学到「答到一半就停」，破坏完整回答。

**练习 4**：一条 `message_content` 为空字符串的数据流经管线时会经历什么？

**答案**：`format_distilled_answer` 抛出 `ValueError("Missing non-empty 'message_content' field.")`，被 `build_examples` 的 `except (KeyError, TypeError, ValueError)` 捕获，`skipped += 1`，其余 11999 条继续处理——单条坏数据不会炸掉整趟构建。

### 4.3 compute_length 与 filter_examples_by_max_len：长度体检与超长过滤

#### 4.3.1 概念说明

拿到 12000 条样本后、送进训练循环前，还差最后一道工序：**长度体检与过滤**。必要性来自两层：

1. **经济层**：训练一个样本的计算与显存近似随长度线性增长（注意力部分更高），超长样本会拖慢整个 epoch、抬高显存峰值。
2. **物理层**：Qwen3 0.6B 的 `context_length` 是 40960（u2-l1 的 `QWEN_CONFIG_06_B`）。notebook 实测最长样本达 **42005 个 token——已经超过模型上下文窗口**，这种样本连一次前向都做不了（u2-l5 讲过，越界输入会直接抛 `ValueError`）。所以过滤不只是省钱，是让数据**合法**。

notebook 的实测数字（[ch08_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/01_main-chapter-code/ch08_main.ipynb) 8.4.3 节输出）：

| 指标 | 全量 12000 条 | 过滤（max_len=2048）后 |
| --- | --- | --- |
| 平均长度 | 2946 tokens | — |
| 最短 | 236（索引 10846） | — |
| 最长 | 42005（索引 2529） | ≤ 2048 |
| 样本数 | 12000 | 6695（删除 5305，约 44%） |

作者选择 `max_len=2048`：牺牲 44% 的样本，换来训练时长与显存可控——ch08/03_logs 里的训练日志文件名 `deepseek-r1-2048_distill_metrics.csv` 中的 "2048" 正是这个阈值。过滤后的样本再经 `random.Random(123)` 洗牌、切出前 25 条做验证集，其余做训练集。

#### 4.3.2 核心流程

```
compute_length(examples, answer_only=False):
    对每条样本取长度 L:
        answer_only=False → L = len(token_ids)          # 总长（prompt + answer + eos）
        answer_only=True  → L = len(token_ids) - prompt_len   # 仅答案区（含 eos）
    打印 平均 / 最短(含索引) / 最长(含索引)
    （只打印，不返回任何值——返回 None）

filter_examples_by_max_len(examples, max_len=2048):
    保留满足 len(token_ids) <= max_len 的样本    # 注意是总长，且 <=（等于也保留）
    打印 Original / Filtered / Removed 三行计数
    返回过滤后的新列表（原列表不动）
```

两个易错点：**过滤判据用的是总长**（含 prompt 与 eos），不是答案长；边界是 `<=` 而非 `<`。

#### 4.3.3 源码精读

**compute_length**——开关决定统计口径：

- [reasoning_from_scratch/ch08.py:L137-L145](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L137-L145)：遍历样本，`answer_only=True` 时长度取 `total - ex["prompt_len"]`——正是下一讲损失要覆盖的答案 token 数（eos 计入其中）；平均值用 `round` 取整。
- [reasoning_from_scratch/ch08.py:L147-L154](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L147-L154)：`min/max` 找最值后用 `lengths.index(...)` 反查索引——方便直接 `examples[longest_idx]` 抽查极端样本；函数只打印不返回。测试 [tests/test_ch08.py:L227-L245](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch08.py#L227-L245) 明确断言 `compute_length(...) is None`，并用 `capsys` 核对打印内容：两条样本 `{"token_ids": [1,2,3], "prompt_len": 1}` 与 `{[1,2,3,4,5], prompt_len: 2}`，总长口径平均 4、answer 口径平均 2——手算一遍即可验证。

**filter_examples_by_max_len**——一行列表推导完成过滤：

- [reasoning_from_scratch/ch08.py:L157-L167](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L157-L167)：保留 `len(s["token_ids"]) <= max_len` 的样本，打印 Original/Filtered/Removed 三行计数后返回新列表。测试 [tests/test_ch08.py:L248-L257](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch08.py#L248-L257) 用 `max_len=3` 验证：长度为 3 的保留（`<=` 边界）、长度为 5 的剔除。

#### 4.3.4 代码实践

**实践目标**：亲手复现长度统计与过滤，并验证边界行为。

**操作步骤**（接 4.2.4 的变量 `examples`，也可独立用小样本）：

```python
# 示例代码：practice_length_filter.py（第 1 部分依赖 4.2.4 的 examples）

# A. 两种统计口径对比
compute_length(examples)                    # 总长口径
compute_length(examples, answer_only=True)  # 答案区口径（含 eos）

# B. 阈值扫描：观察保留数量随 max_len 的变化
for cap in (512, 1024, 2048, 4096):
    kept = filter_examples_by_max_len(examples, max_len=cap)
    print(f"cap={cap}: {len(kept)}")

# C. 边界行为的最小验证（不依赖大数据）
from reasoning_from_scratch.ch08 import filter_examples_by_max_len
tiny = [
    {"token_ids": [1, 2, 3], "prompt_len": 1},   # 长度恰为 3
    {"token_ids": [1, 2, 3, 4, 5], "prompt_len": 2},
]
print(filter_examples_by_max_len(tiny, max_len=3))  # 应保留第一条
```

**需要观察的现象**：

1. A 中两个口径的平均值之差约等于 prompt 模板 + 题面的平均 token 数（几十量级）。
2. B 中保留数量随 cap 单调递增；cap=2048 应得 6695（notebook 实测），其余数值**待本地验证**。
3. C 输出的列表只含第一条样本——长度恰好等于 `max_len` 的样本被保留，确认判据是 `<=`。

**预期结果**：三种观察全部成立即通过；`compute_length` 无返回值（拿不到数字，只能看打印——想程序化处理需自己写等价逻辑）。

#### 4.3.5 小练习与答案

**练习 1**：`answer_only=True` 统计出的长度包含 eos 吗？包含 prompt 吗？

**答案**：包含 eos、不包含 prompt。口径是 `len(token_ids) - prompt_len`，eos 在 `token_ids` 末尾、属于答案区；prompt 的前 `prompt_len` 个 token 被减掉。这个口径恰好等于下一讲交叉熵覆盖的预测目标数。

**练习 2**：过滤判据为什么用**总长**而不是答案长？如果一个极短答案配上超长题面，会被过滤掉吗？

**答案**：因为训练时的前向必须装下**整条序列**（prompt 参与计算、只是不计损失），显存与算力开销由总长决定；总长 42005 的样本会直接超出 40960 的上下文窗口。所以是的——答案再短，题面超长同样会被 `<= max_len` 判掉。

**练习 3**：把 44% 的样本过滤掉，这个决定有什么代价？如果你想在「训练成本」与「数据覆盖」之间折中，动哪个旋钮？

**答案**：代价是分布截断——被删的恰是思考流最长的样本，通常对应更难、步骤更多的题，模型因此见不到长推理链的示范，可能削弱长题表现。旋钮就是 `max_len`：调大（如 4096）保留更多长样本但训练更慢更贵，调小则相反；作者最终取 2048 并把它写进了日志文件名（`deepseek-r1-2048_distill_metrics.csv`）。

## 5. 综合实践

**任务：写一个「蒸馏数据准备脚本」，产出一份带体检报告的训练集文件，供 u9-l3 / u9-l4 直接使用。**

在 4.2.4 单步脚本的基础上，把整条管线固化成 `prepare_distill_data.py`（示例代码）：

```python
# 示例代码：prepare_distill_data.py
# 运行方式: uv run python prepare_distill_data.py
import json
import random

from reasoning_from_scratch.ch08 import (
    load_distill_data, load_reasoning_tokenizer,
    build_examples, compute_length, filter_examples_by_max_len,
)

# 1) 数据与分词器
data = load_distill_data(partition="deepseek-r1-math-train")
tokenizer = load_reasoning_tokenizer()

# 2) 编码 + 体检
examples, skipped = build_examples(data, tokenizer)
print(f"[report] built={len(examples)} skipped={skipped}")
compute_length(examples, answer_only=True)

# 3) 过滤 + 划分（复刻 notebook：Random(123) 洗牌，前 25 条做验证集）
examples = filter_examples_by_max_len(examples, max_len=2048)
random.Random(123).shuffle(examples)
val_examples, train_examples = examples[:25], examples[25:]

# 4) 持久化：token id 是纯 JSON 类型，可直接落盘复用
with open("distill_prepared.json", "w", encoding="utf-8") as f:
    json.dump({"train": train_examples, "val": val_examples}, f)
print(f"[report] train={len(train_examples)} val={len(val_examples)} "
      f"-> distill_prepared.json")
```

验收清单：

1. 运行后得到 `distill_prepared.json`；重新运行时第 1 步显示 `(cached)`、第 2 步结果与首次完全一致（确定性编码的直接证据）。
2. 抽查文件中任意一条：`token_ids[-1]` 等于分词器的 `eos_token_id`；`prompt_len < len(token_ids)`。
3. （可选）仿照 notebook 8.4.3 的直方图 cell，用 matplotlib 对 train/val 画 token 长度分布（`density=True` 归一化，因验证集小得多），确认两个子集分布大体平衡——这正是 notebook 留给读者的习题。

这个脚本就是 u9-l4 训练循环 `train_distillation(model, train_examples, val_examples, ...)` 的直接数据来源。

## 6. 本讲小结

- `load_distill_data`（[ch08.py:L22-L74](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L22-L74)）以「本地缓存优先 + HF 主 URL / Backblaze 备用 URL」加载 2 教师 × 2 题集共四个分区；缓存检查先于白名单断言，本地文件即真相；~105 MB 的 math-train 分区含 12000 条数据。
- `format_distilled_answer` 把教师双流包装成 `<think>{thinking}</think>\n\n{content}` 的单一监督目标；`<think>` 标签让思考流可解析、可隐藏，与 Qwen3 官方推理模型习惯一致。
- `build_examples` 三步走：prompt 套 `render_prompt` + 聊天模板编码，answer 用 `chat_wrapped=False` 裸编码，最后拼 `prompt_ids + answer_ids + [eos]`；坏行逐条捕获计数不炸全局；每条样本只留 `token_ids` 与 `prompt_len` 两个键——「一次编码、多 epoch 复用」。
- `prompt_len` 是下一讲损失切片的锚点：蒸馏损失只对答案区间（含 eos，共 \(T-p\) 个预测目标）计算交叉熵。
- `compute_length` 提供总长/答案区两种口径的体检（只打印不返回）；`filter_examples_by_max_len` 按**总长** `<= max_len` 过滤。实测平均 2946、最长 42005（超 40960 上下文），2048 阈值删掉 44% 换取训练可行。

## 7. 下一步学习建议

本讲产出的 `{"token_ids", "prompt_len"}` 样本下一步就是被「吃掉」：

1. **u9-l3（蒸馏损失）**：精读 [compute_example_loss](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L170-L190)——`input_ids = token_ids[:-1]`、`target_ids = token_ids[1:]` 的错位构造，以及 `answer_start = max(prompt_len - 1, 0)` 如何用本讲记录的 `prompt_len` 把损失限制在答案区。建议带着本讲 4.2.5 练习 2 的结论去读。
2. **u9-l4（训练循环）**：看 `train_distillation` 如何逐条消费这些样本、按 epoch 存 checkpoint，以及 `distill_batched.py` 如何给长短不一的样本 padding 组 batch（过滤阈值与 padding 策略是搭档）。
3. **回看对照**：把本讲管线与第 6 章 RLVR 的数据使用方式对比——同是 12000 题，RLVR 在线 rollout、每题一次，蒸馏离线编码、反复多轮；这条对照线能帮你理解「SFT 与 RL 的工程差异」。
