# 指令生成主循环 instruction_gen.py

## 1. 本讲目标

本讲是「数据生成」流水线的中枢。学完本讲，你应该能够：

- 读懂 `generate_instruction_following_data` 这个**主循环**的整体骨架：它如何一轮轮地调用 GPT、解析响应、去重、落盘，直到凑够目标条数。
- 掌握 `post_process_gpt3_response_uniq` 如何用 `{Instruction}` / `{Input}` 两个**标记**把 GPT 的一段自由文本切成结构化字段。
- 看懂「断点续跑 + tqdm 进度 + save_json 落盘」三者如何配合，让这个可能跑很久的循环可以随时中断、随时恢复。

本讲只解决「主循环怎么转」这一个问题。循环里出现的 ROUGE-L 去重的数学细节留给下一讲（u2-l5），本讲只把它当作一个「返回 True/False 的过滤器」来用。

## 2. 前置知识

在进入源码前，先用三句话回忆前置讲义已经建立的认知（本讲直接承接，不重复展开）：

- **u2-l1**：数据生成分三阶段，仓库只 shipped 了第二阶段「指令变异」的驱动 `instruction_gen.py`；GPT 全程只作数据标注工具，不参与最终模型。
- **u2-l2**：变异提示词模板 `p_example.txt` 用 `#method#` 定下「功能不同、方法/器件相似」的变异策略，并用 `{Instruction}` / `{Input}` 两个占位符规定产出格式。GPT 改写后的输出里会**照抄**这两个占位符作为标记。
- **u2-l3**：`utils.askGPT35` 是全流程唯一与 OpenAI 通信的函数，**始终返回「单元素列表」**，形如 `[{text: ..., finish_reason: ...}]`；当连续 5 次「maximum context」降级后放弃时，返回 `[{'finish_reason': 'length', 'text': ''}]`。

补充两个本讲会用到的 Python 小知识点（给不熟悉的读者）：

- **JSONL（逐行 JSON）**：一个文件里每行是一个独立的 JSON 对象，不是一个大数组。好处是可以流式逐行读、低内存，也能随时追加。
- **猴子补丁（monkeypatch）**：在运行时把一个模块里的函数替换成自己的函数，常用于在不改源码的前提下做测试或 mock。本讲的代码实践会用到它。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们一主一仆，分工明确：

| 文件 | 角色 | 本讲关注的内容 |
| --- | --- | --- |
| `data_generation/instruction_gen.py` | **编排者** | 主循环 `generate_instruction_following_data`、响应解析 `post_process_gpt3_response_uniq`、续跑/进度/落盘 |
| `data_generation/utils.py` | **工具箱** | `askGPT35`（被主循环调用）、`load_json` / `save_json`（落盘与续跑的底层实现） |

一句话总结两者关系：`instruction_gen.py` 负责「流程编排」（加载、循环、解析、去重、落盘），`utils.py` 负责「原子工具」（调 GPT、读写 JSONL）。

> 旁注：`instruction_gen.py` 里还有 `encode_prompt` / `encode_prompt_uniq` / `post_process_gpt3_response` 等函数，它们是 Self-Instruct 原版遗留代码，**当前主循环并未调用**（u2-l2 已说明 `encode_prompt_uniq` 是为注入自定义种子而备）。初读时可以先跳过，避免被干扰。

## 4. 核心概念与源码讲解

### 4.1 主循环 generate_instruction_following_data 整体骨架

#### 4.1.1 概念说明

`generate_instruction_following_data` 是一个**「凑够就停」的 while 循环**。它要做的事情可以一句话讲完：

> 反复请求 GPT 改写出新的 Verilog 题目，每拿到一批就解析、去重，把合格的累积到结果列表里，直到结果列表的长度达到 `num_instructions_to_generate`。

这个设计有两个关键性质：

1. **轮数不确定**：因为每一轮 GPT 可能返回无效内容（格式不对、太短、和已有题目重复），所以「需要调用几次 GPT」事先算不出来，只能用 `while 已生成数量 < 目标数量` 来驱动。
2. **每一轮都落盘**：循环体每跑完一轮就把当前累积结果整体写回文件。这样即使中途断电、Ctrl+C，下次重启也能从文件里读回已生成的部分，接着跑——这就是「断点续跑」。

#### 4.1.2 核心流程

用伪代码把主循环的骨架画出来（省略 ROUGE 的数学，把它当作过滤器）：

```
加载种子数据 seed_instruction_data
if 输出文件已存在:
    target_instruction_data = 读回已生成的指令        # 断点续跑
初始化 tqdm 进度条(total = num_instructions_to_generate)
进度条跳到 len(target_instruction_data)
去重比对池 all_instructions = 种子指令 + 已生成指令

while len(target_instruction_data) < 目标数量:
    prompt = 直接读取模板 p_example.txt
    results = utils.askGPT35(prompt)                # 返回 [{text, finish_reason}]
    本轮合格指令 = []
    for result in results:
        本轮合格指令 += post_process_gpt3_response_uniq(result)   # -> [ans] 或 []

    for entry in 本轮合格指令:
        计算 entry 与去重池的 ROUGE-L 相似度
        if max(相似度) > 0.7:  丢弃              # 近似重复，过滤器拦下
        else:
            keep += 1
            追加 entry 到 target_instruction_data 和去重池
            进度条 +1

    save_json(target_instruction_data, 输出文件)   # 每轮整体重写落盘
```

读这张图时抓住三个「状态变量」就抓住了循环的全部：

- `target_instruction_data`：**结果累积器**，循环终止条件就看它的长度。
- `all_instructions` / `all_instruction_tokens`：**去重比对池**，每接受一条新指令就要追加进去，保证后面的指令和它也比一次。
- `progress_bar`：**进度条**，每接受一条就 `update(1)`。

#### 4.1.3 源码精读

先看函数签名与默认参数：[data_generation/instruction_gen.py:89-97](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L89-L97)

```python
def generate_instruction_following_data(
    output_dir="",
    output_file = 'vary_func_1.json',
    seed_tasks_path="seed_tasks.jsonl",
    num_instructions_to_generate=100,
    num_prompt_instructions=4,
    request_batch_size=1,
):
```

几个要点：

- 真正起作用的参数是 `seed_tasks_path`（种子文件）、`output_file`（输出文件）、`num_instructions_to_generate`（目标条数）。
- `num_prompt_instructions` 在**整个函数体内从未被使用**——它是 Self-Instruct 原版用来决定「每次给 GPT 喂几条种子做 few-shot」的，shipped 的变异流程不依赖它，属于遗留参数。
- `request_batch_size` 名义上是「每轮发几个请求」，但下面会看到它其实是个「哑参数」。

接着是种子加载与断点续跑的入口：[data_generation/instruction_gen.py:98-104](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L98-L104)

```python
seed_instruction_data = utils.load_json(seed_tasks_path)
...
target_instruction_data = []
if os.path.exists(os.path.join(output_dir, output_file)):
    target_instruction_data = utils.load_json(os.path.join(output_dir, output_file))
```

这里先准备一个空列表，再用 `os.path.exists` 判断输出文件是否已存在：存在就 `load_json` 读回，把已完成的指令装回 `target_instruction_data`。这就是续跑的起点——第 4.3 节会展开。

主循环本体从这里开始：[data_generation/instruction_gen.py:117-135](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L117-L135)

```python
while len(target_instruction_data) < num_instructions_to_generate:
    request_idx += 1
    batch_inputs = []
    for _ in range(request_batch_size):
        prompt = open(evo_type).read() + "\n"
        batch_inputs.append(prompt)
    ...
    results = utils.askGPT35(question=batch_inputs[0])
    ...
    instruction_data = []
    for result in results:
        new_instructions = post_process_gpt3_response_uniq(result)
        instruction_data += new_instructions
```

注意三件事：

1. **循环条件**是 `len(target_instruction_data) < num_instructions_to_generate`，每接受一条合格指令才会让这个长度增长；被去重丢弃的不增长。所以如果 GPT 一直产出重复题目，循环不会前进。
2. **批量是个空壳**：`batch_inputs` 凑了 `request_batch_size` 条 prompt，但第 126 行只取 `batch_inputs[0]` 发给 GPT。也就是说无论 `request_batch_size` 设多少，每轮只发 1 个请求。这是初读时最容易误解的地方。
3. **prompt 不注入种子**：第 122 行 `prompt = open(evo_type).read() + "\n"`，只是把模板 `p_example.txt` 原样读出来加个换行，**没有把种子题目拼进去**。所以 GPT 实际上是依据模板里写死的那个「3-stage pipeline」例子在做变异（参见 u2-l2 对模板的分析），而不是依据 `data_sample.json` 里的种子。`encode_prompt_uniq` 那条「把种子拼进模板」的路径在 shipped 主循环里并未启用。

拿到 `results`（永远是单元素列表）后，循环 `for result in results` 逐条交给 `post_process_gpt3_response_uniq` 解析，把解析出的合格指令累加到 `instruction_data`。解析逻辑是第 4.2 节的主题。

接下来是去重块：[data_generation/instruction_gen.py:137-154](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L137-L154)

```python
for instruction_data_entry in instruction_data:
    new_instruction_tokens = scorer._tokenizer.tokenize(instruction_data_entry["Instruction"])
    rouge_scores = [rouge_scorer._score_lcs(new_instruction_tokens,item) for item in all_instruction_tokens]
    rouge_scores = [score.fmeasure for score in rouge_scores]
    ...
    if max(rouge_scores) > 0.7:
        continue
    else:
        keep += 1
    instruction_data_entry["most_similar_instructions"] = most_similar_instructions
    instruction_data_entry["avg_similarity_score"] = float(np.mean(rouge_scores))
    target_instruction_data.append(instruction_data_entry)
    all_instructions.append(instruction_data_entry["Instruction"])
    all_instruction_tokens.append(new_instruction_tokens)
    progress_bar.update(1)
```

本讲只看「流程不看数学」：对每条候选指令，拿它和去重池里所有指令算 ROUGE-L 相似度；只要**最大**相似度超过 `0.7` 就 `continue`（丢弃）。通过过滤的，除了追加进结果列表，还会顺手把两个调试字段 `most_similar_instructions`（最相似的 10 条及其分数）和 `avg_similarity_score`（平均相似度）写进这条记录，方便事后排查「为什么这条被留/被删」。ROUGE-L 的具体计算、`_score_lcs`、阈值为何选 `0.7`，全部留到 u2-l5。

最后每轮结束落盘：[data_generation/instruction_gen.py:159](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L159)

```python
utils.save_json(target_instruction_data, os.path.join(output_dir, output_file))
```

> ⚠️ **一个重要的源码陷阱**：文件末尾的入口调用 [data_generation/instruction_gen.py:165-170](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L165-L170) **没有** `if __name__ == "__main__":` 守卫。这意味着 `import instruction_gen` 就会立刻触发一次真实的 `generate_instruction_following_data(...)`，从而调用真正的 `utils.askGPT35` 去请求 OpenAI。本讲的代码实践必须绕开这个陷阱（见第 5 节）。

#### 4.1.4 代码实践

**实践目标**：用「读 + 算」的方式验证对主循环骨架的理解，不依赖任何外部 API。

**操作步骤**：

1. 打开 [data_generation/instruction_gen.py:117-159](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L117-L159)，在一张纸上画「状态变量变化表」。
2. 假设 `num_instructions_to_generate = 3`，第 1 轮 GPT 返回 1 条合格且不重复的指令，第 2 轮返回 1 条但和已有重复（被 0.7 过滤），第 3 轮返回 1 条合格，第 4 轮返回 1 条合格。
3. 为每一轮填写表格的四个列：`request_idx`、`len(target_instruction_data)`（本轮结束时）、`keep`、`progress_bar` 的已更新次数。

**需要观察的现象**：

- 第 2 轮结束时 `len(target_instruction_data)` **没有增长**（被去重丢弃），但 `request_idx` 仍然 +1，`save_json` 仍然执行了一次（写入的内容和第 1 轮一样）。

**预期结果**：

| 轮次 | request_idx | 结束时 len(target) | keep | 进度条更新次数 |
| --- | --- | --- | --- | --- |
| 起 | 0 | 0 | — | 0 |
| 第 1 轮 | 1 | 1 | 1 | 1 |
| 第 2 轮 | 2 | 1 | 0 | 1 |
| 第 3 轮 | 3 | 2 | 1 | 2 |
| 第 4 轮 | 4 | 3 | 1 | 3（循环退出） |

如果第 2 轮那一格你填成了 `len=2`，说明把「去重丢弃」也计入了结果——这正是初学者最常犯的错。若想确认，待本地验证时可在第 145 行附近加一行 `print("dropped by rouge")` 实际观察。

#### 4.1.5 小练习与答案

**Q1**：如果 GPT 每一轮都返回被 ROUGE 判定为重复的指令，主循环会发生什么？会崩溃吗？

**A**：不会崩溃，但会**死循环**。`while len(target_instruction_data) < num_instructions_to_generate` 的条件永远无法满足，循环会一直跑下去；唯一的好处是每一轮仍会执行 `save_json`，所以已生成的部分不会丢。

**Q2**：把 `request_batch_size` 从 1 改成 5，每轮循环实际会发出几个 GPT 请求？

**A**：**仍然只有 1 个**。第 121-123 行的 `for` 循环会凑出 5 条 prompt 放进 `batch_inputs`，但第 126 行只取 `batch_inputs[0]`，其余 4 条被丢弃。所以 `request_batch_size` 在当前实现里是一个不生效的参数。

**Q3**：`num_prompt_instructions` 这个参数在函数体内被使用了吗？

**A**：没有。它是 Self-Instruct 原版用于 few-shot 种子数量的参数，shipped 的变异流程没有引用它，属于遗留代码。

---

### 4.2 响应解析 post_process_gpt3_response_uniq

#### 4.2.1 概念说明

`askGPT35` 返回的是一段**自由文本**——GPT 会先说一句 "Here is the rewritten prompt"，然后给出改写后的题目。但下游训练需要的是结构化的 `{Instruction, Input}` 字段。`post_process_gpt3_response_uniq` 的职责就是：**从一段自由文本里，切出 Instruction 和 Input 两块内容，并做最基本的质检查**。

它依赖一个关键约定（u2-l2 已铺垫）：模板 `p_example.txt` 要求 GPT 在改写输出里**照抄** `{Instruction}` 和 `{Input}` 这两个占位符当标记。于是解析器只要找到这两个标记，就能像切三明治一样把中间夹的内容取出来。

它的输入输出契约要记牢：

- **输入**：一个 `result` 字典，即 `results` 列表里的**单个元素**，形如 `{"text": ..., "finish_reason": ...}`（注意不是整个列表）。
- **输出**：一个列表，要么是 `[{"Instruction": ..., "Input": ...}]`（单元素，表示解析成功），要么是 `[]`（表示该条无效，应丢弃）。始终返回列表，方便主循环用 `+=` 直接拼接。

#### 4.2.2 核心流程

解析流程是一条「层层放行」的流水线，任何一关不过都返回 `[]`：

```
输入: response = {text, finish_reason}

1. response is None            -> 返回 []
2. finish_reason == "length"   -> 返回 []     # 对应 askGPT35 放弃降级
3. tmp = text 在 '{Instruction}' 处切一刀，取右半段
4. ins = tmp 在 '{Input}' 处切一刀，取左半段并 strip()
5. inp = tmp 在 '{Input}' 处切一刀，取右半段并 strip()
6. 校验: ins 的词数必须 4 ~ 400，且 ins[0] 必须是 ASCII 字符
   不满足 -> 返回 []
7. 通过 -> 返回 [{"Instruction": ins, "Input": inp}]
```

步骤 3-5 用的是 Python 字符串的 `split(marker, 1)`：第二个参数 `1` 表示**最多切 1 刀**，保证即使文本里出现多个 `{Input}` 也只按第一个切。`[0]` 取切刀左边、`[1]` 取切刀右边。

#### 4.2.3 源码精读

完整函数只有十几行：[data_generation/instruction_gen.py:49-66](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L49-L66)

```python
def post_process_gpt3_response_uniq(response):
    if response is None:
        return []
    raw_instructions = response["text"]
    if response["finish_reason"] == "length":
        return []
    try:
        tmp = raw_instructions.split('{Instruction}', 1)[1]
        ins = tmp.split('{Input}', 1)[0].strip()
        inp = tmp.split('{Input}', 1)[1].strip()
        if len(ins.split()) <= 3 or len(ins.split()) > 400:
            return []
        if not ins[0].isascii():
            return []
        ans = {"Instruction": ins, "Input": inp}
        return [ans]
    except:
        return  []
```

逐段拆解：

- **第 50-51 行（None 守卫）**：`askGPT35` 正常不会返回 None，但这里做防御性检查。
- **第 52-54 行（length 守卫）**：这直接承接 u2-l3 讲的那个契约——当 `askGPT35` 连续 5 次「maximum context」降级失败后，会返回 `[{'finish_reason': 'length', 'text': ''}]`。这里一旦看到 `finish_reason == "length"` 就立即丢弃，因为这种情况下 `text` 是空的、没有可用内容。
- **第 55-58 行（切三明治）**：先在 `{Instruction}` 处切，取 `[1]`（标记之后的部分）得到 `tmp`；再在 `{Input}` 处对 `tmp` 切两刀，分别取 `[0]`（两标记之间 = Instruction 正文）和 `[1]`（`{Input}` 之后 = Input 正文）。
- **第 59-62 行（质检）**：两个过滤条件。其一，Instruction 的**词数**（`len(ins.split())`，按空白切词）必须在 4 到 400 之间——太短（≤3 词，比如只有 "Create a module"）说明 GPT 没写完，太长（>400 词）说明可能跑题或粘了别的内容。其二，Instruction **首字符必须是 ASCII**（`ins[0].isascii()`），用来挡掉以非英文字符开头的异常输出。
- **第 63-66 行**：通过所有关卡，包成单元素列表返回。整个 `try` 块的目的是：一旦 `split` 没切出预期片段（例如 GPT 压根没输出 `{Instruction}` 标记，`split(...)[1]` 会抛 `IndexError`），`except` 兜底返回 `[]`，优雅地把「格式不符」的响应当作无效丢弃。

> 为什么用 `split` 而不是正则？因为标记 `{Instruction}` / `{Input}` 是**字面量**（就是大括号包起来的固定字符串），用字符串切分比正则更直白、也不会被特殊字符干扰。这种「用占位符既当填空位又当切分锚点」的设计，是本流程把自由文本变结构化数据的关键技巧。

#### 4.2.4 代码实践

**实践目标**：用几组手工构造的输入，验证解析器在不同边界条件下的行为。

**操作步骤**：写一个独立小脚本（不导入 `instruction_gen`，避免触发前述入口调用陷阱），把函数定义复制出来单独测试：

```python
# 示例代码：parse_check.py
def post_process_gpt3_response_uniq(response):
    if response is None:
        return []
    raw_instructions = response["text"]
    if response["finish_reason"] == "length":
        return []
    try:
        tmp = raw_instructions.split('{Instruction}', 1)[1]
        ins = tmp.split('{Input}', 1)[0].strip()
        inp = tmp.split('{Input}', 1)[1].strip()
        if len(ins.split()) <= 3 or len(ins.split()) > 400:
            return []
        if not ins[0].isascii():
            return []
        return [{"Instruction": ins, "Input": inp}]
    except:
        return []

cases = [
    ("None 输入",      None),
    ("length 放弃",    {"text": "", "finish_reason": "length"}),
    ("缺标记",         {"text": "I will write a module for you.", "finish_reason": "stop"}),
    ("Instruction 太短(3词)", {"text": "{Instruction}\nCreate a module\n{Input}\nmodule m();", "finish_reason": "stop"}),
    ("正常 5 词",      {"text": "Here is it.\n{Instruction}\nPlease act as a professional verilog designer. Create a simple adder module.\n{Input}\nmodule adder(input [7:0] a, input [7:0] b, output [8:0] sum);", "finish_reason": "stop"}),
]
for name, resp in cases:
    print(name, "->", post_process_gpt3_response_uniq(resp))
```

**需要观察的现象**：

- 前四个用例都应返回 `[]`，分别对应 None、length 放弃、缺标记（触发 `IndexError` 被 except 兜底）、词数 ≤ 3。
- 只有第五个「正常 5 词」返回一个非空单元素列表，且 `Instruction` 字段正好是两个标记之间的那段文本。

**预期结果**：

```
None 输入 -> []
length 放弃 -> []
缺标记 -> []
Instruction 太短(3词) -> []
正常 5 词 -> [{'Instruction': 'Please act as a professional verilog designer. Create a simple adder module.', 'Input': 'module adder(input [7:0] a, input [7:0] b, output [8:0] sum);'}]
```

> 「Instruction 太短」用例特意用了恰好 3 词（"Create a module"），用来验证边界是 `<= 3` 即丢弃——也就是**至少要 4 词**才保留。若把词数改成 4 应当通过，待本地验证。

#### 4.2.5 小练习与答案

**Q1**：如果 GPT 返回的 `text` 里完全没有 `{Instruction}` 这串字符，函数会返回什么？为什么？

**A**：返回 `[]`。因为 `raw_instructions.split('{Instruction}', 1)` 在找不到分隔符时返回的是长度为 1 的列表 `['整段文本']`，取 `[1]` 会抛 `IndexError`，被 `except` 捕获后返回空列表。

**Q2**：一条由 5 个英文单词组成的 Instruction 会被保留吗？

**A**：会。过滤条件是 `len(ins.split()) <= 3` 才丢弃，即 4 词及以上通过，5 词满足。

**Q3**：为什么 `finish_reason == "length"` 要直接返回空，而不是试着解析 `text`？

**A**：因为这个标记来自 u2-l3 讲过的 `askGPT35` 放弃路径——连续 5 次「maximum context」降级失败后，`text` 被设成空字符串 `''`，没有任何可解析内容；直接丢弃最安全。

---

### 4.3 断点续跑、tqdm 进度与 save_json 落盘

#### 4.3.1 概念说明

数据生成是一个**长跑任务**（要凑上万条指令，意味着成千上万次 GPT 调用，可能跑几小时甚至几天）。任何一个长跑任务都必须回答两个问题：

1. **中途挂了怎么办？** ——已生成的几千条数据不能白费。
2. **现在跑到哪了？** ——需要一个可见的进度。

RTL-Coder 的解法非常朴素，三件套配合：

- **断点续跑**：启动时先看输出文件存不存在（`os.path.exists`），存在就把里面的数据读回来当作「已完成」，循环从当前长度接着跑。
- **tqdm 进度条**：用 `total = num_instructions_to_generate` 初始化一个进度条，启动时先把已完成的数量 `update` 进去，之后每接受一条新指令再 `update(1)`。
- **每轮落盘**：循环体每一轮都用 `save_json` 把累积结果**整体重写**到输出文件，相当于每轮都保存一份「最新全量快照」。

这套设计的核心洞察是：**「每轮全量重写」天然等价于「随时可恢复」**。因为磁盘上的文件永远是最新状态，下次启动 `load_json` 读回来的就是断点。

#### 4.3.2 核心流程

把三件套在时间轴上对齐：

```
【启动阶段】
  load 种子
  if 输出文件存在:  target = load_json(输出文件)        # 恢复已完成
  progress_bar = tqdm(total = 目标数)
  if target 非空:   progress_bar.update(len(target))    # 进度条跳过已完成

【循环阶段（每一轮）】
  ... 解析、去重、追加到 target ...
  save_json(target, 输出文件)                           # 全量重写
```

这里的 `save_json` / `load_json` 来自 `utils.py`，采用 JSONL 格式（每行一个 JSON 对象）：

- `save_json(dic_list, path)` 用 `'w'`（写覆盖）模式打开文件，**逐行**把列表里每个 dict `json.dumps` 后写入。因为是 `'w'`，每轮都会把旧文件清空再重写整份列表——这正是「全量快照」的实现。
- `load_json(filename)` 用 `'r'` 模式逐行 `json.loads`，拼回列表，内存占用随行数线性增长但流式可读。

#### 4.3.3 源码精读

续跑判断在启动阶段：[data_generation/instruction_gen.py:102-104](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L102-L104)

```python
if os.path.exists(os.path.join(output_dir, output_file)):
    target_instruction_data = utils.load_json(os.path.join(output_dir, output_file))
    print(f"Loaded {len(target_instruction_data)} target  instructions")
```

进度条的初始化与「跳过已完成」：[data_generation/instruction_gen.py:106-108](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L106-L108)

```python
progress_bar = tqdm.tqdm(total=num_instructions_to_generate)
if target_instruction_data:
    progress_bar.update(len(target_instruction_data))
```

注意第 107 行的 `if target_instruction_data:` 判空——只有读回了非空结果才 `update`，否则跳过。配合循环体里第 154 行的 `progress_bar.update(1)`（每接受一条 +1），进度条就能准确反映「距离目标还差多少」。

去重比对池的初始化也在启动阶段，把种子和已生成指令都算进去：[data_generation/instruction_gen.py:109-114](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L109-L114)

```python
all_instructions = [d["Instruction"] for d in seed_instruction_data] + [
    d["Instruction"] for d in target_instruction_data
]
scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=False)
all_instruction_tokens = [scorer._tokenizer.tokenize(inst) for inst in all_instructions]
```

这一段保证续跑时**新生成的指令也会和上一轮已落盘的指令去重**，不会因为重启就产生重复。

每轮落盘这一行已在 4.1 引用过：[data_generation/instruction_gen.py:159](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L159)。它的底层实现是 `utils.save_json`：[data_generation/utils.py:26-31](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L26-L31)

```python
def save_json(dic_list, path):
    with open(path, 'w') as f:
        for dic in dic_list:
            ob = json.dumps(dic)
            f.write(ob)
            f.write('\n')
```

对应的读回实现 `load_json`：[data_generation/utils.py:18-24](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L18-L24)

```python
def load_json(filename):
    des_data = []
    with open(filename, 'r') as f:
        for line in f:
            data = json.loads(line)
            des_data.append(data)
    return des_data
```

两个函数是一对镜像：`save_json` 把列表每个 dict 写成一行，`load_json` 把每行解析回 dict 拼成列表。这就是 JSONL 的「逐行 JSON」格式（u2-l4 数据格式讲义里提到的「Response 统一存成列表、逐行 JSONL」底层正是这两个函数）。

> 设计取舍：每轮全量重写在大数据量下有写放大（第 N 轮要重写 N 行），但因为单轮只新增 1 条、且数据规模是「万条级」而非「百万级」，磁盘 IO 远小于一次 GPT 网络往返，所以这个朴素的「每轮重写」在本场景下完全够用，换取了极简的续跑逻辑。

#### 4.3.4 代码实践

**实践目标**：亲手验证「断点续跑」真的能恢复进度。

**操作步骤**：

1. 在 `data_generation/` 目录下，用 Python 手工构造一个「假的上次结果」文件 `new_instructions.json`，里面写 2 行合法 JSONL，每行形如 `{"Instruction": "...", "Input": "...", "most_similar_instructions": {}, "avg_similarity_score": 0.0}`。
2. 想象此时调用 `generate_instruction_following_data(num_instructions_to_generate=5, output_file='new_instructions.json', ...)`。

**需要观察的现象**（源码阅读型，无需真正运行）：

- 对照第 102-104 行，因为 `new_instructions.json` 已存在，`target_instruction_data` 会被读回这 2 条。
- 对照第 106-108 行，进度条 `total=5`，但立刻 `update(2)`，所以一开始进度就显示 `2/5` 而不是 `0/5`。
- 对照第 109-111 行，去重池 `all_instructions` 一开始就包含这 2 条已生成的 Instruction，保证后续不会和它们重复。
- 主循环条件 `len(target) < 5`，所以只需再凑 3 条就退出。

**预期结果**：续跑从「还需 3 条」开始，而不是从零开始；若真的跑（待本地验证，需配合第 5 节的 mock），你会看到进度条开局就跳到 40%。

#### 4.3.5 小练习与答案

**Q1**：程序被 Ctrl+C 中断后重启，已生成的指令会丢失吗？为什么？

**A**：不会。因为每一轮结束都执行了 `save_json` 把当前全量写回文件。重启时第 102 行的 `os.path.exists` 命中，`load_json` 把已生成的部分读回 `target_instruction_data`，循环从断点继续。

**Q2**：`save_json` 用 `'w'`（覆盖写）每轮重写整个文件，为什么这样还能支持断点续跑？

**A**：因为每轮写入的是**累积的 `target_instruction_data` 全量**，覆盖旧文件等价于保存「最新全量快照」。下次 `load_json` 读回的就是这份最新状态，自然就是断点。如果改成 `'a'`（追加）反而会重复写入已存在的记录，破坏续跑。

**Q3**：如果两个进程同时跑、写同一个 `output_file`，会发生什么？

**A**：会互相覆盖甚至损坏数据。`save_json` 的 `'w'` 会截断文件，两个进程各自只掌握自己内存里的 `target_instruction_data`，互不可见，写入会交错覆盖。该脚本**不支持并发**，同一输出文件只能单进程跑。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个**不调用真实 OpenAI、完全本地可跑**的端到端练习：用 mock 的 `askGPT35` 跑通整个主循环，生成 3 条指令并检查落盘结果。

**背景与陷阱**：由于 `instruction_gen.py` 末尾的入口调用（165-170 行）没有 `if __name__ == "__main__":` 守卫，`import instruction_gen` 会立刻触发一次真实生成（调用真正的 OpenAI）。因此必须**在 import 之前**用猴子补丁替换 `utils.askGPT35`，并按规格要求把目标条数改成 3。

**操作步骤**：

第 1 步——在你的本地工作副本里，把入口的目标条数从 50 改成 3（这是规格要求的改动，仅作用于你本地副本，不影响上游仓库）：

[data_generation/instruction_gen.py:165-170](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L165-L170) 中，把 `num_instructions_to_generate=50` 改为 `num_instructions_to_generate=3`。

第 2 步——在 `data_generation/` 目录下新建 `run_mock.py`（示例代码）：

```python
# run_mock.py （示例代码，必须在 data_generation/ 目录下运行）
import json
import utils

# 1) 准备若干条互不相同的「伪 GPT 输出」，模拟 askGPT35 的返回形状
MOCK_INS = [
    "Please act as a professional verilog designer. Create a module that implements a 4-bit ALU supporting ADD and SUB operations.",
    "Please act as a professional verilog designer. Design a FIFO buffer with depth 16 and width 8, with full and empty flags.",
    "Please act as a professional verilog designer. Build a synchronous 8-bit counter with enable and synchronous reset.",
    "Please act as a professional verilog designer. Create a shift register that shifts left by one bit each clock cycle.",
    "Please act as a professional verilog designer. Design a 4-to-1 multiplexer with 8-bit data inputs and a 2-bit select.",
]
MOCK_INP = [
    "module alu (input clk, input [3:0] a, input [3:0] b, input op, output reg [4:0] y);",
    "module fifo (input clk, input rst, input [7:0] din, output [7:0] dout, output full, output empty);",
    "module counter (input clk, input reset, input enable, output reg [7:0] count);",
    "module shift_reg (input clk, input [7:0] din, output [7:0] dout);",
    "module mux (input [7:0] in0, input [7:0] in1, input [7:0] in2, input [7:0] in3, input [1:0] sel, output [7:0] y);",
]
_state = {"i": 0}

def mock_askGPT35(question, model="gpt-35-turbo", is_response=False, temperature=0.7):
    i = _state["i"] % len(MOCK_INS)
    _state["i"] += 1
    text = ("Here is the rewritten prompt.\n{Instruction}\n"
            + MOCK_INS[i] + "\n{Input}\n" + MOCK_INP[i])
    return [{"text": text, "finish_reason": "stop"}]

# 2) 关键：在 import instruction_gen 之前打补丁
utils.askGPT35 = mock_askGPT35

# 3) 导入即触发文件末尾的入口调用（此时已用 mock、目标数已是 3）
import instruction_gen

# 4) 回读落盘结果，检查每条记录的字段
with open("new_instructions.json") as f:
    for n, line in enumerate(f, 1):
        rec = json.loads(line)
        print(f"--- 记录 {n} ---")
        print("字段:", list(rec.keys()))
        print("Instruction:", rec["Instruction"])
        print("Input     :", rec["Input"])
        print("avg_similarity_score:", rec.get("avg_similarity_score"))
```

第 3 步——安装 u2-l2 提到的「滞后依赖」（这些是 `import instruction_gen` 必需但未写进 requirements.txt 的包），然后运行：

```bash
cd data_generation
pip install tqdm rouge_score numpy
python run_mock.py
```

**需要观察的现象**：

- 控制台会打印每一轮 GPT 调用（被 mock 接管）、ROUGE 处理耗时、`Generated ... kept ...` 日志，以及一个 tqdm 进度条走到 `3/3`。
- 最终打印出 3 条记录，每条的字段应包含 `Instruction`、`Input`、`most_similar_instructions`、`avg_similarity_score` 四个 key——前两个由 `post_process_gpt3_response_uniq` 写入，后两个由去重块（第 149-150 行）写入。
- `Instruction` 字段应以 "Please act as a professional verilog designer." 开头（这是模板 `p_example.txt` 第 4 行要求的固定前缀，GPT——这里是 mock——照抄）。

**预期结果**：`new_instructions.json` 恰好 3 行 JSONL，每行字段一致。若一切正常，你已经用 mock 走完了「调 GPT → 解析 → 去重 → 落盘」的完整闭环。

**待本地验证 / 可能的坑**：

- 因为 5 条 mock 指令共享相同前缀（"Please act as a professional verilog designer."），它们之间的 ROUGE-L 可能偏高。如果出现某条被 `0.7` 阈值误杀导致凑不够 3 条而**死循环**，请把 `MOCK_INS` 里每条的核心电路描述改得更互不相同（例如换完全不同的电路类型），再跑一次。这恰好能让你直观体会 4.1.5 Q1 所说的「重复导致死循环」。
- 若 `import instruction_gen` 报 `FileNotFoundError: 'p_example.txt'`，说明你没在 `data_generation/` 目录下运行——主循环第 122 行用相对路径 `open(evo_type)` 读模板，必须在该目录下执行。

## 6. 本讲小结

- `generate_instruction_following_data` 是一个**「凑够就停」的 while 循环**：反复调 GPT、解析、去重、累积，直到结果长度达到 `num_instructions_to_generate`。循环条件只看 `len(target_instruction_data)`，被去重丢弃的不计数。
- 每轮只发 **1 个** GPT 请求：`request_batch_size` 是个不生效的哑参数，`batch_inputs` 只取 `[0]`；且 shipped 流程**不把种子注入 prompt**，GPT 依据模板内写死的例子变异。
- `post_process_gpt3_response_uniq` 用 `{Instruction}` / `{Input}` 两个**字面量标记**像切三明治一样从自由文本里取出字段，并用 `finish_reason`、词数 4-400、首字符 ASCII 三道关卡质检，任何一关不过或切分失败都返回 `[]`。
- **断点续跑三件套**：`os.path.exists` + `load_json` 恢复已完成指令，`tqdm` 进度条开局先跳过已完成数量，`save_json` 每轮用 `'w'` 全量重写落盘——三者让长跑任务可随时中断恢复。
- 底层 `save_json` / `load_json` 采用 **JSONL（逐行 JSON）** 格式，是生成、训练、评分三阶段共用的读写基础。
- ⚠️ `instruction_gen.py` 末尾的入口调用**没有** `if __name__ == "__main__":` 守卫，`import` 即触发真实生成；做本地实验必须先用猴子补丁替换 `utils.askGPT35`。

## 7. 下一步学习建议

本讲把「主循环怎么转」讲完了，但循环里那一段 ROUGE-L 去重是被当作黑盒用的。下一讲 **u2-l5「基于 ROUGE-L 的指令去重」** 会打开这个黑盒，讲清 `rouge_scorer.RougeScorer(['rougeL'])`、`_score_lcs` 最长公共子序列、`fmeasure` 相似度以及 `0.7` 阈值是怎么决定一条候选指令被留还是被删的。

如果你想先跳到训练侧，可以直接去 **u2-l6「训练方案总览与共享数据管线」**，但要记得：本讲产出的 `new_instructions.json`（格式 `{Instruction, Input, Response[...]}`）正是后续训练数据的上游，理解本讲的落盘格式对读懂训练侧的 `ScoreDataset` / `DataCollator` 有直接帮助。
