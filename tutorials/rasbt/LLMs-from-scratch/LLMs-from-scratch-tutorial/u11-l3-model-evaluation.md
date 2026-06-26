# 模型评估：用 Ollama / OpenAI 评分

## 1. 本讲目标

学完本讲后，你应当能够：

- 理解为什么「损失 / 困惑度」不足以衡量一个指令微调模型好不好用，并建立 **LLM-as-a-judge（让大模型当裁判）** 的评估范式。
- 掌握用 **Ollama 本地模型（Llama 3 8B）** 与 **OpenAI API（GPT-4）** 两种方式，对一批模型响应批量打分。
- 理解并计算三种相关性指标（**Pearson / Spearman / Kendall Tau**），用「两个裁判是否一致」来论证自动评分是否可信。

本讲是整个学习手册的收官评估篇：前面 u7-l2 已经把模型微调好并产出了带响应的测试集，本讲回答「这个微调后的模型到底答得好不好」。

## 2. 前置知识

本讲依赖 u7-l2（指令微调）的产出，并需要你大致了解以下概念：

- **指令微调（instruction finetuning）**：把预训练语言模型教成「能听懂指令并作答」的助手，详见 u7-l1 / u7-l2。
- **损失与困惑度**：预训练阶段用交叉熵损失 / 困惑度衡量「下一个 token 预测得准不准」，详见 u5-l1。本讲的核心动机恰恰是：**这套指标无法衡量回答的「有用性」**。
- **REST API 与 JSON**：Ollama 暴露一个本地 HTTP 接口，我们用 Python 的 `requests` 库收发 JSON。
- **相关性（correlation）**：两组数字之间的「同涨同跌」程度，取值在 \(-1\) 到 \(1\) 之间，越接近 \(1\) 越一致。

> 不需要 GPU：本讲的 Ollama 方案在普通笔记本（如 M3 MacBook Air）上即可跑通，全书也是这样设计的。

## 3. 本讲源码地图

本讲涉及的源码全部位于 `ch07/03_model-evaluation/` 与 `ch07/01_main-chapter-code/`，核心文件如下：

| 文件 | 作用 |
| --- | --- |
| [`ch07/01_main-chapter-code/ollama_evaluate.py`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/ollama_evaluate.py) | 命令行版 Ollama 评分脚本，是本讲最干净、行号最清晰的入口。包含 `query_model`、`format_input`、`generate_model_scores`、`main`。 |
| [`ch07/03_model-evaluation/llm-instruction-eval-ollama.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/03_model-evaluation/llm-instruction-eval-ollama.ipynb) | 交互式 notebook 版 Ollama 评分，带逐步讲解与样本输出，是教学主入口。 |
| [`ch07/03_model-evaluation/llm-instruction-eval-openai.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/03_model-evaluation/llm-instruction-eval-openai.ipynb) | OpenAI GPT-4 版评分，结构与 Ollama 版一一对应，用于对比「云端裁判」。 |
| [`ch07/03_model-evaluation/scores/correlation-analysis.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/03_model-evaluation/scores/correlation-analysis.ipynb) | 评分相关性分析：计算 Pearson/Spearman/Kendall，验证「两个裁判是否一致」。 |
| [`ch07/03_model-evaluation/eval-example-data.json`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/03_model-evaluation/eval-example-data.json) | 示例评估数据，含两个候选模型的响应，共 100 条。 |
| `ch07/01_main-chapter-code/instruction-data-with-response.json` | u7-l2 指令微调模型的实际测试响应，是本讲代码实践与综合实践的打分对象。 |

> 说明：`ollama_evaluate.py` 与 Ollama notebook 的核心函数（`query_model` / `format_input` / `generate_model_scores`）几乎逐行相同，前者是把 notebook 逻辑封装成的可命令行运行脚本。本讲引用源码时，行号精确的引用一律指向 `.py` 文件；notebook 作为交互式阅读入口，按 cell 内容说明，不强行标注行号（notebook 行号随渲染方式变化、不可靠）。

---

## 4. 核心概念与源码讲解

### 4.1 指令响应评估：为什么需要 LLM-as-a-judge

#### 4.1.1 概念说明

u5-l1 学过：预训练语言模型用**交叉熵损失**和**困惑度**来打分。但到了指令微调阶段，这套指标会「失灵」——

- 困惑度衡量的是「模型给标准答案的概率有多高」，是一种**字面匹配**视角；
- 可指令问答的好与坏，往往是**语义层面**的：「氯的元素符号答成 Cl 还是 C」是对错问题；「把这句话改得更正式」则是开放问题，根本没有唯一标准答案。

**人工评估（human evaluation）** 是金标准：请人逐条读模型回答并打分。但它又慢又贵，100 条数据请人标注就要数小时，且不同标注者之间也有分歧。于是工业界普遍采用 **LLM-as-a-judge（让大模型当裁判）**：用一个能力较强的 LLM（如 GPT-4 或 Llama 3 8B）充当自动裁判，读「指令 + 标准答案 + 模型回答」后给出 0–100 的分数。它比人工便宜几个数量级，又比纯字面指标更接近「人类感受」。

但自动裁判本身也可能是错的，所以本讲第 4.3 节要专门回答：**这个裁判到底可不可信？** 方法是看它和另一个裁判（或人工）的打分是否高度一致。

#### 4.1.2 核心流程

LLM-as-a-judge 的标准流水线如下：

1. **准备评估数据**：每条样本至少包含 `instruction`（指令）、`input`（可选补充输入）、`output`（标准答案）、`model_response`（被评模型的回答）。
2. **拼裁判提示词（scoring prompt）**：把「指令、标准答案、待评回答」塞进一段固定模板，要求裁判只输出一个 0–100 的整数。
3. **调用裁判模型**：用 Ollama 或 OpenAI 接口拿到裁判的文本回答。
4. **解析整数分数**：用 `int(score)` 把裁判回答转成整数；解析失败则跳过（或记 0）。
5. **聚合**：对 100 条分数取平均，得到模型的总体质量分。

数据格式约定见 `eval-example-data.json`（带两个候选模型）：

```python
{
    "instruction": "Calculate the hypotenuse of a right triangle with legs of 6 cm and 8 cm.",
    "input": "",
    "output": "The hypotenuse of the triangle is 10 cm.",               # 标准答案
    "model 1 response": "\nThe hypotenuse of the triangle is 3 cm.",    # 候选模型 1 的回答
    "model 2 response": "\nThe hypotenuse of the triangle is 12 cm."    # 候选模型 2 的回答
}
```

#### 4.1.3 源码精读：`format_input` 与裁判提示词

裁判模型需要先看到「指令长什么样」，这部分由 `format_input` 负责。它复用了 u7-l1 中的 **Alpaca 风格模板**，把指令与可选输入拼成结构化文本：

[ch07/01_main-chapter-code/ollama_evaluate.py:51-60](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/ollama_evaluate.py#L51-L60) —— 把 `instruction` 和（若有）`input` 拼成统一的指令文本，供裁判阅读上下文。

```python
def format_input(entry):
    instruction_text = (
        f"Below is an instruction that describes a task. "
        f"Write a response that appropriately completes the request."
        f"\n\n### Instruction:\n{entry['instruction']}"
    )
    input_text = f"\n\n### Input:\n{entry['input']}" if entry["input"] else ""
    return instruction_text + input_text
```

注意 `input_text` 的三元写法：当 `entry["input"]` 为空串时整段省略，保证裁判只看到与该问题真正相关的部分。

真正决定打分口径的是**裁判提示词**，它出现在 `generate_model_scores` 内部（详见 4.2.3）。其关键设计有两点：

1. **给出参照系**：同时告诉裁判「正确答案 `output`」和「待评回答」，让裁判做对比式打分，而不是凭空评分。
2. **强制整数输出**：句尾加 `Respond with the integer number only.`（只回复整数），这样后续 `int(score)` 才能稳定解析。这点很关键——notebook 里去掉这句的早期版本，裁判会输出一长段理由（如 `I'd score this response as 0 out of 100.`），`int()` 直接抛 `ValueError`。

#### 4.1.4 代码实践：理解评估数据与提示词

1. **目标**：不调用任何模型，纯靠阅读理解评估数据的结构和裁判提示词的构造。
2. **操作步骤**：
   - 打开 [`eval-example-data.json`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/03_model-evaluation/eval-example-data.json)，找到第 1 条（勾股定理那条）。
   - 在脑中（或纸上）对它跑一遍 `format_input`，写出拼好的指令文本。
   - 再套上裁判提示词模板，写出完整的打分提示词。
3. **需要观察的现象**：标准答案是 10 cm，而 `model 1 response` 写的是 3 cm、`model 2 response` 写的是 12 cm——两个候选答案都错，但裁判该如何区分？
4. **预期结果**：裁判提示词最终形如：
   > `Given the input \`<format_input 的结果>\` and correct output \`The hypotenuse of the triangle is 10 cm.\`, score the model response \`The hypotenuse of the triangle is 3 cm.\` on a scale from 0 to 100, where 100 is the best score. Respond with the integer number only.`
   你会发现两个错误回答数值上离 10 cm 一样远（都差 2），但裁判可能因为措辞、单位等给出不同分数——这正是 LLM-as-a-judge 与字面匹配的本质区别。
5. 待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能直接用交叉熵损失来评估指令微调模型？

<details><summary>参考答案</summary>

交叉熵损失衡量的是「模型给标准答案 token 的概率」，属于字面匹配。但指令问答的好坏是语义层面、甚至是开放性的（如「把句子改得更正式」没有唯一正确答案）。两个语义完全等价的回答可能用词不同，字面损失差异巨大，却应当得到相近的分数。所以需要 LLM-as-a-judge 这类语义评估。
</details>

**练习 2**：评估数据里 `output`（标准答案）和 `model_response`（待评回答）分别扮演什么角色？如果评估一个**没有标准答案**的开放式生成任务（如写诗），这套流水线要怎么改？

<details><summary>参考答案</summary>

`output` 是裁判打分的参照系（金标准），`model_response` 是被评对象。没有唯一标准答案时，可改为 **成对比较（pairwise）** 模式：不再问「这个回答得几分」，而是让裁判同时看两个模型的回答、判断「哪个更好」，再累计胜率（如 MT-Bench、Chatbot Arena 做法）。本讲用的是 **逐条打分（pointwise）** 模式。
</details>

---

### 4.2 用 Ollama / OpenAI 当裁判批量打分

#### 4.2.1 概念说明

「裁判」可以由任意一个足够强的 LLM 担任。本讲提供两种实现，接口形态不同、逻辑同构：

- **Ollama（本地裁判）**：把模型（如 Llama 3 8B）下载到本机，通过本地 REST API（默认 `http://localhost:11434/api/chat`）收发请求。**免费、离线、可复现**，缺点是受限于本机算力，裁判能力上限是 8B/70B 量级。
- **OpenAI（云端裁判）**：调用 OpenAI 的 Chat Completions API（默认 `gpt-4-turbo`）。裁判更强，但**收费、需联网、需 API Key**，全书跑完约 200 次评估花费约 0.26 美元。

两者都强调一个关键词：**确定性（determinism）**。裁判打分必须尽量可复现，否则今天测出来 80 分、明天变成 60 分，评估就失去意义。两个实现都用 `temperature=0` + `seed=123` 来逼近确定性，但——

> **重要现实**：即便设了 `temperature=0` 和 `seed`，Ollama 在不同操作系统之间、OpenAI 的 GPT-4 仍**不是完全确定性**的。所以本讲不依赖单条分数，而是对 **100 条取平均**，用统计稳定性对冲随机性。

#### 4.2.2 核心流程

以 Ollama 版为例：

1. **确认 Ollama 在运行**：`check_if_running("ollama")` 用 `psutil` 扫描进程，没起来就直接报错。
2. **构造请求**：把裁判提示词放进 `{"model": "llama3", "messages": [...], "options": {"seed":123, "temperature":0, "num_ctx":2048}}` 这个 payload。
3. **流式接收**：Ollama 的 `/api/chat` 是**逐行流式**返回（每行一个 JSON 片段），`requests` 用 `stream=True` + `iter_lines` 拼接 `message.content`。
4. **批量循环**：`generate_model_scores` 遍历全部样本，对每条调一次裁判、解析整数、`tqdm` 显示进度条。
5. **汇总平均**：`main` 打印 `平均分 = sum(scores)/len(scores)`。

OpenAI 版把「步骤 2–3」换成一次 `client.chat.completions.create(...)`，其余完全一致。

#### 4.2.3 源码精读

**(1) `query_model` —— 调用 Ollama REST API**

[ch07/01_main-chapter-code/ollama_evaluate.py:14-39](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/ollama_evaluate.py#L14-L39) —— 向本地 Ollama 发送 chat 请求，关键是 `options` 里的确定性设置与流式拼接逻辑。

```python
def query_model(prompt, model="llama3", url="http://localhost:11434/api/chat"):
    data = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "options": {     # Settings below are required for deterministic responses
            "seed": 123,
            "temperature": 0,
            "num_ctx": 2048
        }
    }
    with requests.post(url, json=data, stream=True, timeout=30) as r:
        r.raise_for_status()
        response_data = ""
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            response_json = json.loads(line)
            if "message" in response_json:
                response_data += response_json["message"]["content"]
    return response_data
```

要点：
- `seed=123` + `temperature=0`：逼近确定性输出。
- `num_ctx=2048`：上下文窗口设为 2048，保证「指令+答案+回答+裁判要求」放得下。
- `stream=True`：Ollama 把回答切成多个 JSON 行返回，这里逐行 `json.loads` 并拼接 `content`，最终还原成完整文本。

**(2) `run_chatgpt` —— 调用 OpenAI API（notebook 版）**

Ollama notebook 与 OpenAI notebook 的 `format_input`、裁判提示词完全相同，唯一差别在「怎么调裁判」。OpenAI 版用一个客户端对象（`client`）一次性拿结果：

```python
# 来自 llm-instruction-eval-openai.ipynb
def run_chatgpt(prompt, client, model="gpt-4-turbo"):
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        seed=123,
    )
    return response.choices[0].message.content
```

对比可见：OpenAI 不需要手动流式拼接，库已封装好；但同样靠 `temperature=0.0` + `seed=123` 追求确定性。`client` 由 `OpenAI(api_key=...)` 构造，密钥从同目录 `config.json` 读取（注意保密，不要提交到 git）。

**(3) `generate_model_scores` —— 批量打分与容错**

[ch07/01_main-chapter-code/ollama_evaluate.py:79-99](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/ollama_evaluate.py#L79-L99) —— 遍历全部样本调裁判，把文本回答解析成整数分数；这是 `.py` 版相对 notebook 版的增强点。

```python
def generate_model_scores(json_data, json_key, model="llama3"):
    scores = []
    for entry in tqdm(json_data, desc="Scoring entries"):
        if entry[json_key] == "":        # ← .py 版新增：空回答直接记 0 分
            scores.append(0)
        else:
            prompt = (
                f"Given the input `{format_input(entry)}` "
                f"and correct output `{entry['output']}`, "
                f"score the model response `{entry[json_key]}`"
                f" on a scale from 0 to 100, where 100 is the best score. "
                f"Respond with the integer number only."
            )
            score = query_model(prompt, model)
            try:
                scores.append(int(score))
            except ValueError:
                print(f"Could not convert score: {score}")
                continue
    return scores
```

读这段代码时抓住三个细节：

1. **空回答兜底**：`if entry[json_key] == ""` 分支是 `.py` 脚本版相对 notebook 版**新增**的——若模型没生成回答，直接记 0 分而非跳过，保证分数列表长度与样本数一致（notebook 版无此分支，因此 model 2 只收回 99 个分）。
2. **整数强制**：句尾 `Respond with the integer number only.` 配合 `int(score)` 解析；万一裁判仍返回长文本，`except ValueError` 捕获并跳过该条。
3. **可复用性**：`json_key` 参数让同一个函数既能评 `model 1 response` 也能评 `model 2 response`，是复用关键。

**(4) `main` 与命令行入口 —— 串起来**

[ch07/01_main-chapter-code/ollama_evaluate.py:63-76](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/ollama_evaluate.py#L63-L76) —— 加载 JSON、检查 Ollama 是否在跑、对 `model_response` 这一列打分、打印平均分。

[ch07/01_main-chapter-code/ollama_evaluate.py:42-48](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/ollama_evaluate.py#L42-L48) —— `check_if_running` 用 `psutil` 扫描进程名，确认 Ollama 服务已启动，否则直接抛 `RuntimeError`。

[ch07/01_main-chapter-code/ollama_evaluate.py:102-119](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/ollama_evaluate.py#L102-L119) —— `argparse` 命令行入口，用 `--file_path` 指定要打分的 JSON 文件，文件须含 `output` 与 `model_response` 两个键。

> **接口契约**：`ollama_evaluate.py` 要求输入 JSON 每条含 `instruction`、`input`（可空）、`output`、`model_response` 四个键——这恰好就是 u7-l2 产出的 `instruction-data-with-response.json` 的格式。所以 u7-l2 的产出可以直接喂给本脚本打分，无需任何转换。这是本讲综合实践的连接点。

#### 4.2.4 代码实践：用 Ollama 给一条回答打分

1. **目标**：跑通 Ollama 本地服务，用 `query_model` 思路给一条回答打分，验证裁判流水线可用。
2. **操作步骤**：
   - 按 [Ollama 官网](https://ollama.com) 安装 Ollama；在终端运行 `ollama serve`（或启动 Ollama 应用）。
   - 另开一个终端，运行 `ollama run llama3`（8B 模型，约 4.7 GB，首次自动下载）。
   - 在 Python 里复刻 notebook 的冒烟测试，确认连通：
     ```python
     # 示例代码（来自 llm-instruction-eval-ollama.ipynb 的连通性测试）
     import json, requests
     def query_model(prompt, model="llama3", url="http://localhost:11434/api/chat"):
         data = {"model": model,
                 "messages": [{"role": "user", "content": prompt}],
                 "options": {"seed": 123, "temperature": 0, "num_ctx": 2048}}
         with requests.post(url, json=data, stream=True, timeout=30) as r:
             r.raise_for_status()
             out = ""
             for line in r.iter_lines(decode_unicode=True):
                 if line:
                     j = json.loads(line)
                     if "message" in j:
                         out += j["message"]["content"]
         return out
     print(query_model("What do Llamas eat?"))
     ```
   - 连通后，直接用脚本给 u7-l2 的模型响应打分：
     ```bash
     python ch07/01_main-chapter-code/ollama_evaluate.py \
         --file_path ch07/01_main-chapter-code/instruction-data-with-response.json
     ```
3. **需要观察的现象**：冒烟测试应返回一段关于「Llamas 吃什么」的连贯英文（证明 Llama 3 已可用）；脚本运行时 `tqdm` 显示 `Scoring entries` 进度条，逐条调裁判。
4. **预期结果**：脚本最后打印 `Number of scores: N of N` 与 `Average score: XX.XX`。具体分数随你本机的微调模型质量而定，**待本地验证**；通常 u7 用 gpt2-medium (355M) 微调 2 轮得到的回答，平均分会在一个中等区间。
5. 若没有跑过 u7-l2 的微调、拿不到 `instruction-data-with-response.json`，可改用仓库自带的示例数据 [`eval-example-data.json`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/03_model-evaluation/eval-example-data.json)，但其键名是 `model 1 response` / `model 2 response`，需要把 `main` 里的 `json_key="model_response"` 改成对应键名（或用 notebook 版的循环分别评两个模型）。

#### 4.2.5 小练习与答案

**练习 1**：`.py` 版的 `generate_model_scores` 比同名 notebook 版多了一个 `if entry[json_key] == "": scores.append(0)` 分支。这个分支解决了什么问题？

<details><summary>参考答案</summary>

它处理「模型回答为空」的情况，直接记 0 分并保证 `scores` 列表长度等于样本数。notebook 版没有这个分支，空回答会走到 `else` 里正常拼提示词、调裁判；更关键的是，没有它时若某条解析失败被 `continue` 跳过，分数总数会小于样本数（notebook 实测 model 2 只收回 99/100），影响平均分的口径。`.py` 版用「空回答记 0」让结果列表长度稳定。
</details>

**练习 2**：裁判提示词结尾为什么要加 `Respond with the integer number only.`？去掉会怎样？

<details><summary>参考答案</summary>

为了让裁判只输出一个整数，使 `int(score)` 能稳定解析。去掉后，裁判会输出一长段理由（如 `I'd score this response as 0 out of 100. The correct answer is...`），`int()` 遇到非纯数字字符串直接抛 `ValueError`，该条分数就被 `except` 跳过，最终能收回的有效分数大幅减少。notebook 早期可视化版本（没加这句话）正是这样产生冗长回答的。
</details>

---

### 4.3 评分相关性分析：裁判可信吗

#### 4.3.1 概念说明

LLM-as-a-judge 的最大疑问是：**裁判自己也可能评错**。怎么判断一个自动裁判是否可信？经典做法是**拿两个裁判互相印证**（或与人工评分对照），看它们打分是否高度一致——这就是**相关性分析**。

本讲用的是 [`scores/correlation-analysis.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/03_model-evaluation/scores/correlation-analysis.ipynb)，它比较 **GPT-4** 与 **Llama 3 8B** 对同一批（model 1）响应的打分。直觉是：如果连两个完全不同的裁判都给出高度一致的排序，那这套自动评分就很可能抓住了「真正的质量」。

有三种主流相关系数，**它们衡量「一致」的角度不同，缺一不可**：

- **Pearson（皮尔逊）相关系数**：衡量**线性**相关。它是两组数值协方差除以各自标准差，对异常值敏感、且假定关系是线性的：

\[ r = \frac{\sum_i (x_i - \bar{x})(y_i - \bar{y})}{\sqrt{\sum_i (x_i - \bar{x})^2}\,\sqrt{\sum_i (y_i - \bar{y})^2}} \]

- **Spearman（斯皮尔曼）秩相关**：先把两组分数各自转成**排名（rank）**，再对排名算 Pearson。它衡量的是**单调**关系（一个升、另一个也升即可，不必等比例），对异常值更鲁棒，更适合 0–100 这种带排序意味的分。

- **Kendall Tau（肯德尔 τ）**：数所有样本对中「同序对」与「异序对」的数量差，再除以总对数。它直接刻画**排序一致性**，最稳健，对小样本和并列值友好：

\[ \tau = \frac{\#\text{concordant pairs} - \#\text{discordant pairs}}{\binom{n}{2}} \]

三者取值都在 \([-1, 1]\)：越接近 \(1\) 表示两个裁判越一致。经验上 \(\tau\) 数值通常最小、Pearson 最大（因尺度敏感），所以三个一起看才全面。

#### 4.3.2 核心流程

`correlation-analysis.ipynb` 的流程很简洁：

1. **加载两组分数**：分别读取 `gpt4-model-1-response.json` 与 `llama3-8b-model-1-response.json`（各 100 个整数），得到两个等长列表 `list1`、`list2`。
2. **画散点 + 拟合线**：`plt.scatter` 画散点，`np.poly1d(np.polyfit(list1, list2, 1))` 画一条一阶（线性）回归线，直观判断一致性。
3. **算三种系数**：`np.corrcoef`（Pearson）、`scipy.stats.spearmanr`、`scipy.stats.kendalltau`。
4. **对照基准**：与 Prometheus 2 论文（Kim et al. 2024）中专用评估模型的相关系数比较，判断 Llama 3 8B 作为裁判是否「够用」。

仓库提供的两组分数前若干个长这样（`llama3-8b-model-1-response.json` 开头）：

```python
[20, 92, 85, 90, 20, 90, 22, 97, 60, 96, ...]   # 共 100 个
```

#### 4.3.3 源码精读

`correlation-analysis.ipynb` 的核心计算 cell（按 cell 描述引用，不标行号）：

```python
import numpy as np, pandas as pd
from scipy.stats import spearmanr, kendalltau

list1, list2 = gpt4_model_1, llama3_8b_model_1   # GPT-4 分 vs Llama3-8B 分

pearson_correlation = np.corrcoef(list1, list2)[0, 1]
spearman_correlation, _ = spearmanr(list1, list2)
kendall_tau_correlation, _ = kendalltau(list1, list2)

correlation_table = pd.DataFrame({
    "Pearson": [pearson_correlation],
    "Spearman": [spearman_correlation],
    "Kendall Tau": [kendall_tau_correlation]
}, index=['Results'])
```

用仓库自带的两组分数，跑出来的结果是（见 notebook 输出）：

| | Pearson | Spearman | Kendall Tau |
| --- | --- | --- | --- |
| GPT-4 vs Llama 3 8B | **0.805** | **0.698** | **0.573** |

怎么解读这张表？三个层次：

1. **符号与量级**：三个值都显著为正且不算小（0.57–0.80），说明 GPT-4 和 Llama 3 8B 对「哪个回答好」的判断高度同向——一个给高分的，另一个也倾向给高分。
2. **Pearson > Spearman > Kendall**：符合理论预期。Pearson 直接用原始数值（0–100 的线性关系），数值最大；Spearman 用排名，抹掉了尺度；Kendall 只看两两排序，最保守，所以最小。三个一起报，是为了避免单一指标误导。
3. **与基准对比**：notebook 还贴了 Prometheus 2 论文的大表。论文里专门为「打分评估」微调的模型（如 PROMETHEUS-2-8X7B）在多个基准上 Pearson 普遍在 0.55–0.69 区间。本讲用**未做评估微调**的现成 Llama 3 8B 就拿到 0.805 的 Pearson，**已进入甚至超过专用评估模型的量级**——这有力地支持了「用本地 8B 模型当裁判」这一做法的合理性。

> **承接关键结论**：4.2 节两个裁判对 `model 1` / `model 2` 的平均分都给出「model 1 明显更好」的相同结论（Ollama：78.48 vs 64.98；OpenAI：74.09 vs 56.57），本节又给出 0.805 的 Pearson 一致性。两份证据相互印证：即便裁判能力有限、且不完全确定性，**只要在足够多样本上取平均并交叉验证，自动评估依然可信**。

#### 4.3.4 代码实践：复现相关性，并与人工评分对照

1. **目标**：亲手复现三种相关系数，并把「裁判」与「人工」对齐，体会哪种系数更稳健。
2. **操作步骤**：
   - 进入 `ch07/03_model-evaluation/scores/`，运行 [`correlation-analysis.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/03_model-evaluation/scores/correlation-analysis.ipynb)，复现 GPT-4 vs Llama 3 8B 的 0.805 / 0.698 / 0.573。
   - **扩展（人工对照，推荐）**：从 100 条里抽 20 条，由你自己当「人工裁判」逐条打 0–100 分，存成 `human-model-1-response.json`；再算 `human` 与 `gpt4`、`human` 与 `llama3` 的三种系数。
3. **需要观察的现象**：
   - GPT-4 vs Llama 3 8B 的散点应大致沿对角线分布，但有离群点（两裁判分歧大的个别样本）。
   - 引入人工分后，通常 Pearson 仍最高，Kendall 最低；当个别样本两裁判分歧很大时，Pearson 被拉低得比 Spearman 更狠，可见 Spearman/Kendall 的鲁棒性。
4. **预期结果**：复现值应与 0.805 / 0.698 / 0.573 接近（因 Ollama 跨 OS 不完全确定，允许小幅偏差）；人工对照的相关系数若也达到 0.6 以上，即可认为该自动裁判「可用」。**待本地验证**。
5. 若无 OpenAI 额度，可跳过 GPT-4，仅做 `llama3` 与 `human` 的对照——这恰好对应本讲综合实践里「与人工评分做相关性分析」的要求。

#### 4.3.5 小练习与答案

**练习 1**：为什么报告相关性时通常**同时给 Pearson、Spearman、Kendall 三个**，而不是只给一个？

<details><summary>参考答案</summary>

三者衡量一致性的角度不同：Pearson 看**线性**关系且对异常值敏感；Spearman 看**单调**关系（基于排名），更鲁棒；Kendall 看**两两排序**一致性，最稳健、对小样本友好。只看 Pearson 会被少数离群样本带偏；只看 Kendall 又会低估整体线性强度。三者一起报，才能全面判断两个裁判在「线性、单调、排序」三个层面是否都一致。
</details>

**练习 2**：本节算的是 GPT-4 与 Llama 3 8B **两个自动裁判**之间的一致性。为什么「两个自动裁判一致」仍不能 100% 等同于「评分正确」？怎样才能更接近「正确」这一结论？

<details><summary>参考答案</summary>

两个自动裁判可能**犯同类的系统性错误**（例如都偏好更长、更啰嗦的回答，即 length bias），彼此一致≠与人类一致。要更接近「正确」，应把自动裁判的分数与**人工标注**做相关性分析——人工才是金标准。本节用「两个裁判互相印证」是一种成本较低的折中验证；notebook 对照 Prometheus 2 论文数据，也是借外部人工标注基准来旁证可信度。
</details>

---

## 5. 综合实践

把本讲三块内容串成一个完整的「评估—验证」闭环，直接服务 u7-l2 的微调模型。

**任务**：对你（u7-l2）微调出的指令模型，用 Ollama 自动打分，并与人工评分做相关性分析，输出相关系数。

**步骤**：

1. **准备响应数据**：确认 `ch07/01_main-chapter-code/instruction-data-with-response.json` 存在（u7-l2 产出，键为 `instruction/input/output/model_response`）。若没有，先回 u7-l2 跑一遍微调生成它。
2. **自动打分**：用 4.2.4 的命令对它跑 `ollama_evaluate.py`，得到 100 个 Llama 3 裁判分，存为 `llama3-sft-response.json`（可仿照 notebook 把 `generate_model_scores` 的返回 `json.dump` 落盘）。
3. **人工打分**：从中随机抽 30 条，由你本人当人工裁判，按同样的 0–100 口径打分，存为 `human-sft-response.json`（注意与自动分的样本**顺序一一对应**，这是算相关性的前提）。
4. **相关性分析**：仿照 4.3.3 的代码，计算 `llama3` 分与 `human` 分的 Pearson / Spearman / Kendall，并画散点图。
5. **下结论**：根据相关系数（一般 Pearson ≥ 0.6、Kendall ≥ 0.4 视为「可用」），判断你的微调模型评估是否可信，并写出 1–2 句结论。

**验收**：输出三个相关系数；若与人工的一致性达标，说明 Llama 3 裁判对你的模型质量排序可信，可用它代替人工做更大规模的评估。具体数值**待本地验证**。

---

## 6. 本讲小结

- **损失 / 困惑度衡量不了指令回答的「有用性」**，因此需要 **LLM-as-a-judge**：用一个强 LLM 读「指令+标准答案+模型回答」打 0–100 分。
- 评分数据的最小契约是 `instruction/input/output/model_response` 四键；裁判提示词靠 `format_input`（Alapaca 模板）拼上下文，并强制裁判 `Respond with the integer number only.` 以便 `int()` 解析。
- **Ollama（本地 llama3，免费离线）与 OpenAI（云端 gpt-4-turbo，收费）两种裁判逻辑同构**，都靠 `temperature=0`+`seed=123` 追求确定性，但都**不完全确定**，故对 100 条取平均来对冲随机性。
- `generate_model_scores` 是批量打分核心，`.py` 版比 notebook 版多一个「空回答记 0」分支；`try/except ValueError` 容忍裁判偶尔输出非纯数字。
- **判断裁判可信靠相关性分析**：Pearson（线性）/ Spearman（单调秩）/ Kendall（排序）三者角度不同需同报；仓库实测 GPT-4 vs Llama 3 8B 的 Pearson 高达 0.805，已媲美专用评估模型。
- 两个裁判对「model 1 优于 model 2」给出一致平均分，加上高相关性，**双重证据**证明：在足够样本上取平均并交叉验证，自动评估可信。

## 7. 下一步学习建议

- **横向阅读**：本讲用了逐条打分（pointwise）。想了解成对比较（pairwise），可搜索 MT-Bench、Chatbot Arena 的做法，它们更适合无标准答案的开放生成。
- **裁判质量进阶**：阅读 notebook 引用的 **Prometheus 2 论文（Kim et al. 2024, arXiv:2405.01535）**，看如何专门微调一个「评估模型」，以及更多基准上的人工相关性数据。
- **回到训练侧**：如果评估发现模型回答有系统性偏差（如 length bias、答非所问），可回 u11-l2（DPO 偏好对齐）用偏好数据纠偏，形成「评估→对齐→再评估」的闭环。
- **工程化扩展**：尝试把本讲的 Ollama 评分封装进一个 CI 脚本，让每次微调后自动跑一遍评估并记录平均分，追踪模型版本质量演化。
