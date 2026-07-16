# Distilabel 数据生成流水线

## 1. 本讲目标

本讲承接 [u1-l2 仓库目录结构与核心入口](u1-l2-repo-structure.md)。在那一讲里我们提到：open-r1 的「三步走」计划中，Step 1 是**蒸馏**——用一个已经会推理的强模型，大批量生成「带思维链的回答」，再把这些回答当作训练数据去 SFT 一个小模型。那么问题来了：**这批「带思维链的回答」从哪里来？**

答案就是本讲的主角：`src/open_r1/generate.py`。它不训练任何模型，而是调用 [Distilabel](https://github.com/argilla-io/distilabel) 这个数据合成库，把一批题目（prompt）喂给一个推理模型，收集模型产出的推理过程，最后打包成一个数据集。换句话说，它是 R1 流水线最上游的「数据工厂」。

学完本讲，你应当能够：

- 说清 `build_distilabel_pipeline` 这个函数是如何用「代码」而不是 YAML，把一条 `Pipeline` 装配出来的，以及 `Pipeline().ray()` 背后那套 Ray 分布式机制在做什么。
- 解释 `TextGeneration` 步骤里 `OpenAILLM`、`template`、`input_mappings`、`num_generations`、`group_generations`、`StepResources` 这几个关键参数各自的作用。
- 看懂 `generate.py` 作为命令行工具（CLI）的完整执行链路：解析参数 → 加载数据集 → 跑流水线 → `distiset.push_to_hub`。
- 区分 open-r1 生产用的 `generate.py` 与 README 里那个「smol 示例」在架构上的关键差别（一个连远端 vLLM 服务、一个在进程内跑 vLLM）。

## 2. 前置知识

### 2.1 什么是 Distilabel

[Distilabel](https://github.com/argilla-io/distilabel) 是 Argilla 团队开源的「数据合成/标注流水线」库。它的核心抽象是 **Pipeline（流水线）**：一条流水线由若干 **Step（步骤）** 拼成，步骤之间像 DAG 一样首尾相连；每个 Step 可以接一个 **LLM** 去做实际的生成。

你可以把它理解成一个「数据加工车间」：

| 概念 | 类比 |
|---|---|
| `Pipeline` | 整条流水线 / 车间 |
| `Step`（如 `TextGeneration`） | 一个工位，负责一道工序 |
| `LLM`（如 `OpenAILLM`） | 工位上干活的「工人」 |
| 输入数据集 | 送进车间的原料 |
| `distiset`（输出） | 加工好的成品 |

open-r1 没有用 distilabel 自带的命令行或 YAML 来描述流水线，而是**用 Python 函数 `build_distilabel_pipeline` 把它「写」出来**。这样做的好处是：参数可以由上层（命令行 / Slurm 脚本）动态注入，便于规模化。

### 2.2 什么是「vLLM 服务」与 OpenAI 兼容接口

vLLM 是一个高性能的推理引擎。它有一条 `vllm serve` 命令，可以把一个模型**部署成一个 HTTP 服务**，并且这个服务对外暴露的是**和 OpenAI API 完全一致的接口**（`/v1/completions`、`/v1/chat/completions` 等）。

这一点非常关键：正因为接口兼容，distilabel 的 `OpenAILLM` 才能连到一个**本地的 vLLM 服务**，把它当成「OpenAI」来用——哪怕它实际跑的是 DeepSeek-R1。所以你会看到 `generate.py` 里写了一个看起来很假的 `api_key="something"`：本地 vLLM 服务根本不校验密钥，但 `OpenAILLM` 的构造函数要求必须传一个，于是随便填一个占位字符串。

> 对照：README 里的「smol 示例」走的是另一条路——它用 `distilabel.models.vLLM` 直接在**当前进程内**启动 vLLM，不经过任何 HTTP 服务。这两条路我们在第 5 节的综合实践里会专门对比。

### 2.3 什么是 Ray

[Ray](https://www.ray.io/) 是一个分布式计算框架。distilabel 的 `Pipeline().ray()` 表示「这条流水线要在 Ray 上跑」。Ray 既能在**单机**上自动起一个本地集群，也能在**多机**上把若干节点组成一个大集群。

open-r1 的 `generate.py` 一律走 `Pipeline().ray()`，也就是说**哪怕你只有一台机器，它也会启用 Ray 后端**。这是它与 README smol 示例（普通 `Pipeline()`、不起 Ray）的又一个差别，也是新手跑 `generate.py` 时容易踩的坑——你得先装好 `ray`（好在 `setup.py` 的依赖 `distilabel[vllm,ray,openai]>=1.5.2` 已经把 `ray` 带进来了）。

### 2.4 prompt template 与 Jinja2

`TextGeneration` 步骤用一个 Jinja2 模板把数据集里的字段渲染成最终喂给模型的文本。最简单的模板就是 `"{{ instruction }}"`，意思是「把 `instruction` 这个字段的值原样放进来」。模板里还可以写死一些指令前缀，例如 README 示例里的：

```text
You will be given a problem. Please reason step by step, and put your final answer within \boxed{}:
{{ instruction }}
```

理解了模板，你才能理解接下来要讲的 `input_mappings`——它负责把**数据集里本来的列名**映射到模板里用的 `instruction`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/open_r1/generate.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py) | 本讲的核心。提供 `build_distilabel_pipeline()` 这个「流水线装配函数」，同时自身也可作为命令行脚本运行 |
| [slurm/generate.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/generate.slurm) | 多机生产脚本：在 Slurm 集群上拉起 Ray 集群 → 用 Ray job 启动 vLLM 服务 → 再用 Ray job 提交 `generate.py` |
| [README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md) | 「Data generation」一节给出了可单机运行的 smol 示例，是本讲代码实践的直接依据 |
| [setup.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.py) | 声明 `distilabel[vllm,ray,openai]>=1.5.2` 这一依赖，说明 vllm / ray / openai 三个 extras 都要用到 |

## 4. 核心概念与源码讲解

### 4.1 `build_distilabel_pipeline`：用函数把一条流水线「装配」出来

#### 4.1.1 概念说明

distilabel 描述一条流水线有两种常见写法：用 YAML 配置，或者用 Python 代码。open-r1 选了后者——它把所有可调参数都做成函数入参，由一个叫 `build_distilabel_pipeline` 的函数「按需」装配出一条 `Pipeline` 对象返回。

这种设计的好处是：**装配逻辑只写一次，但参数可以从命令行、Slurm 脚本、甚至别处 import 后复用**。事实上 `generate.py` 自己的 CLI（4.3 节）就是先解析命令行参数，再转手调用这个函数。

这条流水线只有**一个**步骤：`TextGeneration`（文本生成）。它做的事用一句话概括就是：**对输入数据集里的每一条 prompt，让指定的 LLM 生成若干条回答**。

#### 4.1.2 核心流程

`build_distilabel_pipeline` 的执行过程可以拆成三步：

1. **拼装生成参数 `generation_kwargs`**：以 `max_new_tokens` 为基础，只有当用户显式传了 `temperature` / `top_p` 时才把它们加进去（保持「不传就不用」）。
2. **进入 `Pipeline().ray()` 上下文**：在 Ray 后端上建立一条流水线 `pipeline`，随后在 `with` 块里声明的所有 Step 都会自动挂到这条流水线上。
3. **声明唯一的 `TextGeneration` 步骤**：把 LLM、模板、批大小、生成份数、并行副本数等全部配置好，然后 `return pipeline`。

用伪代码表示：

```text
def build_distilabel_pipeline(model, base_url, prompt_column, prompt_template, ...):
    generation_kwargs = {"max_new_tokens": max_new_tokens}
    if temperature is not None: generation_kwargs["temperature"] = temperature
    if top_p       is not None: generation_kwargs["top_p"]       = top_p

    with Pipeline().ray() as pipeline:      # 启用 Ray 后端
        TextGeneration(                      # 唯一一个步骤
            llm=OpenAILLM(base_url, model, generation_kwargs, ...),
            template=prompt_template,
            input_mappings={"instruction": prompt_column} 若 prompt_column 非空,
            num_generations=...,
            group_generations=True,
            resources=StepResources(replicas=client_replicas),
        )
    return pipeline
```

#### 4.1.3 源码精读

函数签名集中了所有「旋钮」，默认值反映了 open-r1 的典型用法：连本地 `http://localhost:8000/v1` 的 vLLM 服务、每条最多生成 8192 token、默认只生成 1 份、单副本：

[src/open_r1/generate.py:23-36](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L23-L36) —— `build_distilabel_pipeline` 的全部入参及默认值。注意 `base_url` 默认就是本地 vLLM 服务的地址。

`generation_kwargs` 的「条件拼装」是这里最值得注意的细节：**没传 `temperature` 就不往里放 `temperature`**，于是最终会使用 vLLM 服务端自己的默认温度，而不是被 Python 端覆盖成某个固定值：

[src/open_r1/generate.py:37-43](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L37-L43) —— 只有 `max_new_tokens` 一定带；`temperature`、`top_p` 仅在非 `None` 时加入。

接下来是装配流水线的核心三行：`Pipeline().ray()` 开启 Ray 后端、`with` 块里声明 `TextGeneration`、函数末尾把流水线返回：

[src/open_r1/generate.py:45-63](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L45-L63) —— 在 `Pipeline().ray()` 上下文里声明 `TextGeneration` 步骤并返回 `pipeline`。

这里有几个**容易看漏但很重要**的点，先列出，后面 4.2 节会逐一展开：

- `OpenAILLM(..., api_key="something", ...)`：连的是本地 vLLM，密钥随便填。
- `input_mappings={"instruction": prompt_column}`：把数据集的列重命名进步骤期望的 `instruction` 输入。
- `num_generations` + `group_generations=True`：每个 prompt 生成多份回答，并**打包成列表**而不是摊成多行。
- `resources=StepResources(replicas=client_replicas)`：把这一步复制出多份并行跑。

#### 4.1.4 代码实践（源码阅读 / 干装配型）

这是一个**不需要 GPU** 的实践：只装配流水线对象、不真正运行生成，用来直观感受 `build_distilabel_pipeline` 返回了什么。

1. **实践目标**：在不触发任何模型推理的前提下，调用 `build_distilabel_pipeline` 拿到一个 `Pipeline` 对象，并观察它的结构。
2. **操作步骤**：

   先确认环境里装了 distilabel（含 ray extras），然后在项目根目录执行：

   ```python
   # 文件名：probe_pipeline.py（示例代码，你自己创建）
   import os
   os.environ.setdefault("PYTHONPATH", "src")  # 让本地 src/open_r1 可被 import

   from open_r1.generate import build_distilabel_pipeline

   pipe = build_distilabel_pipeline(
       model="deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
       prompt_column="problem",            # 对应 NuminaMath 的列名
       prompt_template="{{ instruction }}",
       num_generations=2,
   )
   print(type(pipe))
   print(pipe)
   ```

3. **需要观察的现象**：打印出的对象类型应是 distilabel 的 `Pipeline`；其字符串表示里应能看到一个 `TextGeneration` 步骤，以及它内部挂着的 `OpenAILLM`、`base_url=http://localhost:8000/v1`、`num_generations=2` 等信息。
4. **预期结果**：你拿到的是一个**已配置好但尚未运行**的流水线对象。由于 `Pipeline().ray()` 会在装配时进入 Ray 后端，若环境未安装 `ray`，这一步会直接报 `ImportError` / 模块缺失——这正是「`generate.py` 一律走 Ray」的第一个证据。
5. **待本地验证**：`Pipeline().ray()` 是否在「装配阶段」就初始化 Ray 连接（还是推迟到 `pipeline.run()`），不同 distilabel 版本行为可能不同，请以你本地实际报错/输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `temperature=0.0` 传进 `build_distilabel_pipeline`，`generation_kwargs` 里会不会包含 `temperature` 键？为什么？
**答案**：会。判断条件是 `if temperature is not None`，而 `0.0 is not None` 为真，所以会写入 `generation_kwargs["temperature"] = 0.0`。这提醒我们：「不传」和「传 0」在这里是两回事——前者沿服务端默认，后者强制贪心解码。

**练习 2**：为什么 `build_distilabel_pipeline` 要做成「函数返回 Pipeline」，而不是直接在模块顶层写死一条流水线？
**答案**：因为参数（模型名、地址、模板、生成份数、副本数等）需要由调用方动态注入。做成函数后，CLI（4.3 节）和 Slurm 脚本都能在解析完参数后复用同一段装配逻辑，避免重复代码。

### 4.2 `TextGeneration` 步骤：OpenAILLM、模板与 input_mappings

#### 4.2.1 概念说明

`TextGeneration` 是 distilabel 内置的一个「文本生成」步骤。它的工作周期是：

1. 从上游（这里是输入数据集）接收一个**批**的数据行。
2. 按 `input_mappings` 把数据行里的字段**改名**成步骤期望的输入（默认期望一个叫 `instruction` 的字段）。
3. 用 `template` 把每条 `instruction` 渲染成最终 prompt 文本。
4. 把这批 prompt 交给挂载的 LLM（这里是 `OpenAILLM`）去生成。
5. 根据 `num_generations` 和 `group_generations` 决定输出行的形态。

这里有两套「批量」概念容易混淆，先讲清楚：

- **`input_batch_size`**（步骤级）：`TextGeneration` 每次向 LLM 提交多少条 prompt。它影响的是单次请求的吞吐与显存占用。
- **`dataset_batch_size`**（流水线级，见 4.3 节 `pipeline.run(...)` 的入参）：整条流水线每次从数据集里取多少行喂给第一个步骤。`generate.py` 在 CLI 里把它设成了 `input_batch_size * 1000`。

#### 4.2.2 核心流程

`TextGeneration` 的关键在 `num_generations` 与 `group_generations` 如何决定**输出的行数与形状**。设输入数据集有 \(N\) 行、每个 prompt 生成 \(G\) 份回答，则：

\[
|\text{输出行数}| =
\begin{cases}
N & \text{若 } \texttt{group\_generations} = \text{True} \\
N \times G & \text{若 } \texttt{group\_generations} = \text{False}
\end{cases}
\]

open-r1 选了 `group_generations=True`，于是**输出行数等于输入行数**，每行新增一个 `generations` 字段，里面是一个长度为 \(G\) 的列表，装着这一条 prompt 的全部 \(G\) 份回答。这种「一组回答打包」的形态，正好是后续做拒绝采样（rejection sampling）或 GRPO 分组打分想要的输入格式。

`input_mappings` 的作用可以图示为：

```text
数据集行: {"problem": "...", "solution": "..."}
                │  input_mappings={"instruction": "problem"}
                ▼
步骤输入: {"instruction": "...", "solution": "..."}   # problem 被改名成 instruction
                │  template = "{{ instruction }}"
                ▼
渲染后 prompt: "..."                                  # 只取 instruction 渲染
```

注意：`input_mappings` 只是**改名**，不会删掉其它列；模板只挑它需要的字段来渲染。

最后，`resources=StepResources(replicas=client_replicas)` 把这个 `TextGeneration` 步骤复制成 `client_replicas` 份**并行**执行，相当于给这个工位安排多个工人，用来压榨更大的并发——在面对 vLLM 服务的吞吐上限时很有用。

#### 4.2.3 源码精读

`OpenAILLM` 这一段是「连本地 vLLM」的全部秘密。注意三个细节：`base_url` 指向 vLLM 的 OpenAI 兼容端口、`api_key` 是占位符、`timeout` 与 `max_retries` 控制单次请求的健壮性：

[src/open_r1/generate.py:46-54](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L46-L54) —— `OpenAILLM` 把对「OpenAI」的调用重定向到本地 vLLM 服务。

`template`、`input_mappings`、`input_batch_size`、`num_generations`、`group_generations`、`resources` 这一组参数共同定义了「怎么取数据、怎么渲染、生成几份、是否打包、并行几份」：

[src/open_r1/generate.py:55-61](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L55-L61) —— `TextGeneration` 步骤的关键配置。`input_mappings` 在 `prompt_column is None` 时退化为空字典，即不做任何改名。

> **小提示（参数默认值不一致）**：`build_distilabel_pipeline` 函数签名里 `timeout` 默认是 `900`（[L34](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L34)），但 CLI 的 `--timeout` 默认却是 `600`（[L150](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L150)）。也就是说「直接 import 调用函数」和「走命令行」得到的超时不一样，调参时要注意你走的是哪条路径。

#### 4.2.4 代码实践（纯 Python 模拟，无需 GPU）

这个实践用普通 Python **模拟** `input_mappings` + `group_generations` 对数据形状的影响，帮助你直观理解 4.2.2 的公式，不需要任何模型或 GPU。

1. **实践目标**：用代码验证「同一批输入，在 `group_generations` 开/关两种情况下输出行数不同」。
2. **操作步骤**：

   ```python
   # 示例代码：模拟 TextGeneration 的输出形状
   N = 10          # 输入行数（对应 NuminaMath 取 10 条）
   G = 2           # num_generations

   # 1) input_mappings 的改名效果
   row = {"problem": "求 1+1", "solution": "2"}
   mapping = {"instruction": "problem"}   # 与 generate.py 一致
   mapped = {mapping.get(k, k): v for k, v in row.items()}
   print("改名后:", mapped)               # {'instruction': '求 1+1', 'solution': '2'}

   # 2) group_generations 开/关的输出行数
   group_true  = N                         # 每行的 G 份回答打包成列表
   group_false = N * G                     # 每份回答摊成独立一行
   print(f"group=True  输出行数: {group_true}")
   print(f"group=False 输出行数: {group_false}")
   ```

3. **需要观察的现象**：改名后键名从 `problem` 变成了 `instruction`；`group=True` 输出 10 行、`group=False` 输出 20 行。
4. **预期结果**：与 4.2.2 的公式一致：\(N=10, G=2\) 时打包为 10 行（每行一个长度 2 的列表），摊开为 20 行。
5. 本实践为纯逻辑模拟，结果确定，无需「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：README 的 smol 示例里 `num_generations=4`，对 10 条数据最终产出多少条生成？如果用 open-r1 的 `generate.py`（`group_generations=True`、`num_generations=4`）呢？
**答案**：smol 示例同样把 `num_generations=4` 传给 `TextGeneration`，共 \(10 \times 4 = 40\) 条生成；但其打包方式取决于该示例自身的 `group_generations` 设置。open-r1 的 `generate.py` 因 `group_generations=True`，**行数仍是 10**，只是每行的 `generations` 列表里装 4 条。换句话说：「生成条数」和「输出行数」是两件事。

**练习 2**：为什么 `OpenAILLM` 的 `api_key` 写成 `"something"` 也能工作？
**答案**：因为它连的是本地 vLLM 的 OpenAI 兼容服务，该服务不校验 API Key；`OpenAILLM` 构造函数只是强制要求传一个非空字符串，所以随便填一个占位值即可。

### 4.3 argparse CLI 与 `distiset.push_to_hub`

#### 4.3.1 概念说明

`generate.py` 除了导出 `build_distilabel_pipeline` 给别人 import，自己还带一个 `if __name__ == "__main__":` 块，可以**当命令行工具直接跑**。它的职责是：

1. 用 `argparse` 把命令行参数解析成 Python 变量。
2. 用 `datasets.load_dataset` 加载输入数据集。
3. 调 `build_distilabel_pipeline` 装配流水线。
4. `pipeline.run(...)` 跑流水线，拿到一个 `distiset`（distilabel 的数据集产物）。
5. 如果用户指定了 `--hf-output-dataset`，就把 `distiset` 推到 Hugging Face Hub。

注意它**不复用** trl 的 `TrlParser` / 三元组配置（那是 `sft.py`、`grpo.py` 才用的），而是用标准库 `argparse`——因为数据生成不涉及训练参数与模型加载参数那套体系，参数更扁平。

#### 4.3.2 核心流程

CLI 的执行链路：

```text
argparse 解析 --hf-dataset / --model / --num-generations / ...
        │
        ▼
load_dataset(hf_dataset, hf_dataset_config, split=hf_dataset_split)
        │
        ▼
build_distilabel_pipeline(model, base_url=vllm_server_url, ...)
        │
        ▼
pipeline.run(dataset, dataset_batch_size=input_batch_size*1000, use_cache=False)  → distiset
        │
        ▼ 若指定了 --hf-output-dataset
distiset.push_to_hub(hf_output_dataset, private=private)
```

几个关键点：

- `--hf-dataset` 和 `--model` 是**必填**的（其余都有默认值）。
- `--prompt-column` 的默认值是 `"prompt"`；但 NuminaMath 类数据集的列名是 `problem`，所以跑这类数据时**必须显式** `--prompt-column problem`，否则 `input_mappings` 会去找一个不存在的列。
- `pipeline.run` 的 `dataset_batch_size` 被设成 `input_batch_size * 1000`，即流水线层面一次喂入大批数据，让 Ray 后端有足够并行度。
- `use_cache=False` 表示不读取 distilabel 自身的缓存，强制重新生成。
- `push_to_hub` 是**可选**的：不传 `--hf-output-dataset` 就只本地产出、不推送。

#### 4.3.3 源码精读

`argparse` 把每一个命令行开关对应到 `build_distilabel_pipeline` 的一个入参，并给出一组默认值。注意 `--timeout` 默认 `600`（与函数签名的 `900` 不同，见 4.2.3 小提示）：

[src/open_r1/generate.py:66-171](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L66-L171) —— 命令行参数定义与解析。

加载输入数据集后，把解析到的参数转手喂给装配函数，得到流水线：

[src/open_r1/generate.py:178-195](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L178-L195) —— `load_dataset` 之后调用 `build_distilabel_pipeline`，注意 `base_url` 来自 `--vllm-server-url`。

真正「跑」流水线、并按需推送 Hub 的收尾逻辑：

[src/open_r1/generate.py:197-208](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L197-L208) —— `pipeline.run(...)` 返回 `distiset`；若给了 `--hf-output-dataset` 则 `distiset.push_to_hub(...)`。

对应到 Slurm 集群上的「生产用法」，`generate.py` 是被当作一个 Ray job 提交的，连到的是集群上另一个 Ray job 部署的 vLLM 服务：

[slurm/generate.slurm:218-238](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/generate.slurm#L218-L238) —— 在 Ray 集群里提交 `generate.py`，并用 `--vllm-server-url` 指向集群内的 vLLM 服务端口 `8000`。

而那个 vLLM 服务本身，也是同一个脚本用 `ray job submit` 拉起的，跨节点做 tensor/pipeline 并行：

[slurm/generate.slurm:176-187](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/generate.slurm#L176-L187) —— 用 `vllm serve` 把模型部署成 OpenAI 兼容服务，`--tensor-parallel-size` 取每节点 GPU 数，`--pipeline-parallel-size` 取节点数。

#### 4.3.4 代码实践（源码阅读型 + 可选真跑）

这个实践既包含「读懂调用链」的源码阅读部分，也给出「真跑」的可选路径（需 GPU）。

1. **实践目标**：把命令行参数一路追踪到 `pipeline.run`，并尝试用 `generate.py` 的函数对一个本地 vLLM 服务跑 10 条 NuminaMath、各生成 2 条推理。
2. **操作步骤（源码阅读部分，无需 GPU）**：
   - 打开 [src/open_r1/generate.py:182-202](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L182-L202)，对照下表，把每个 CLI 参数追到它在 `build_distilabel_pipeline` 或 `pipeline.run` 里的落点：

     | CLI 参数 | 落点 |
     |---|---|
     | `--model` | `build_distilabel_pipeline(model=...)` → `OpenAILLM(model=...)` |
     | `--vllm-server-url` | `build_distilabel_pipeline(base_url=...)` → `OpenAILLM(base_url=...)` |
     | `--prompt-column` | `build_distilabel_pipeline(prompt_column=...)` → `input_mappings` |
     | `--num-generations` | `build_distilabel_pipeline(num_generations=...)` → `TextGeneration(num_generations=...)` |
     | `--input-batch-size` | 既进 `input_batch_size`，又 × 1000 进 `dataset_batch_size` |
     | `--hf-output-dataset` | 末尾 `distiset.push_to_hub(...)` |

3. **操作步骤（真跑部分，需 GPU + 已起的 vLLM 服务）**：
   - 先在另一个终端起 vLLM 服务（OpenAI 兼容）：

     ```bash
     vllm serve deepseek-ai/DeepSeek-R1-Distill-Qwen-7B --max-model-len 8192
     # 或：trl vllm-serve --model deepseek-ai/DeepSeek-R1-Distill-Qwen-7B
     ```

   - 再运行 `generate.py`，对 NuminaMath 取 10 条、每条生成 2 份（注意 `--prompt-column problem`）：

     ```bash
     PYTHONPATH=src python -m open_r1.generate \
         --hf-dataset AI-MO/NuminaMath-TIR \
         --hf-dataset-split train \
         --prompt-column problem \
         --model deepseek-ai/DeepSeek-R1-Distill-Qwen-7B \
         --num-generations 2 \
         --max-new-tokens 8192
     ```

     > 说明：`generate.py` 当前 CLI 会加载**整个 split**，没有「只取 10 条」的开关。若只想验证 10 条，建议改用第 5 节综合实践里那种「自己 import `build_distilabel_pipeline` + `dataset.select(range(10))`」的写法。
4. **需要观察的现象**：控制台先打印全部参数、再打印「Loading dataset」「Running generation pipeline」「Generation pipeline finished!」；最终 `distiset` 里每行带一个长度为 2 的 `generations` 列表。
5. **预期结果**：在没有 `--hf-output-dataset` 时，流水线产出的 `distiset` 只留在内存/本地，不推送；若加了该参数则推到 Hub。
6. **待本地验证**：真跑部分依赖具体 GPU、vLLM 与 Ray 环境，生成内容因模型与采样参数而异，请以本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：CLI 默认的 `--prompt-column` 是 `prompt`，但 README 的 NuminaMath 示例用的列名是 `problem`。如果忘了加 `--prompt-column problem` 会发生什么？
**答案**：`input_mappings` 会变成 `{"instruction": "prompt"}`，而 NuminaMath 没有 `prompt` 列，distilabel 在运行时会因找不到该列而报错。所以跑 NuminaMath 类数据集时务必显式指定 `--prompt-column problem`。

**练习 2**：为什么 `pipeline.run` 里要设 `use_cache=False`？
**答案**：distilabel 支持把步骤的输出缓存起来以断点续跑。生成推理数据时通常希望每次都重新采样（尤其带温度），关掉缓存可避免读到上一次的旧结果，确保拿到的是本次参数下的全新生成。

## 5. 综合实践

把本讲三个最小模块串起来：用 `build_distilabel_pipeline` 连一个本地（或远端）vLLM 服务，对 **10 条 NuminaMath 数据各生成 2 条推理**，并打印结果。这正是任务规格里要求的实践。

> 设计参考：README 的「smol 示例」（[README.md:669-726](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L669-L726)）提供了数据集（`AI-MO/NuminaMath-TIR`、`.select(range(10))`）、prompt 模板（带 `\boxed{}` 指令）和 `num_generations` 用法的样例。**但请注意架构差异**：smol 示例用进程内 `distilabel.models.vLLM`、不起 Ray；而我们这里按 open-r1 生产路径走 `build_distilabel_pipeline`（`OpenAILLM` 连外部 vLLM 服务 + `Pipeline().ray()`）。

**步骤 1：装依赖、起 vLLM 服务（需 GPU）**

```bash
uv pip install "distilabel[vllm,ray,openai]>=1.5.2"
# 在另一个终端起 OpenAI 兼容的 vLLM 服务
vllm serve deepseek-ai/DeepSeek-R1-Distill-Qwen-7B --max-model-len 8192
```

**步骤 2：写一个驱动脚本，调 `build_distilabel_pipeline` 并跑 10×2**

```python
# 文件名：run_smol_generation.py（示例代码，你自己创建）
from datasets import load_dataset
from open_r1.generate import build_distilabel_pipeline

# 取 NuminaMath 前 10 条（与 README smol 示例一致）
dataset = load_dataset("AI-MO/NuminaMath-TIR", split="train").select(range(10))

prompt_template = (
    "You will be given a problem. Please reason step by step, "
    "and put your final answer within \\boxed{}:\n{{ instruction }}"
)

pipeline = build_distilabel_pipeline(
    model="deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
    base_url="http://localhost:8000/v1",   # 本地 vLLM 服务（远端则换成对应地址）
    prompt_column="problem",                # NuminaMath 的列名
    prompt_template=prompt_template,
    temperature=0.6,
    max_new_tokens=8192,
    num_generations=2,                      # 每条生成 2 份推理
    client_replicas=1,
)

distiset = pipeline.run(dataset=dataset, dataset_batch_size=64, use_cache=False)

# 打印第一条 prompt 的两份生成
row = distiset["default"]["train"][0]        # distilabel distiset 的典型结构，待本地确认分片名
print("PROMPT:", row.get("instruction") or row.get("problem"))
for i, gen in enumerate(row["generations"]):
    print(f"--- generation {i} ---")
    print(gen)
```

**步骤 3：观察与预期**

- 由于 `group_generations=True`、`num_generations=2`，输出应有 **10 行**，每行的 `generations` 是长度为 2 的列表（共 20 条推理）。
- 每条推理应包含思维链过程，且因 `temperature=0.6` 同一 prompt 的两份回答通常不同。
- 想要保存到 Hub，可在脚本末尾加 `distiset.push_to_hub("username/numina-smol-r1")`（需先 `huggingface-cli login`）。

**无 GPU 的替代方案（源码阅读型综合实践）**：若本地无 GPU，请改为完成 4.3.4 的「源码阅读部分」参数追踪表，并手绘一张数据流图：`CLI 参数 → build_distilabel_pipeline → OpenAILLM/TextGeneration → pipeline.run → distiset → push_to_hub`，标注每一步用到的关键字段。标注「待本地验证」即可。

> 待本地验证：上述真跑依赖具体 GPU、vLLM 版本与 Ray 环境；distilabel `distiset` 的分片名（如 `default`）与列名（如 `instruction` 是否保留）在不同版本下可能有差异，请以本地实际结构为准。

## 6. 本讲小结

- `src/open_r1/generate.py` 是 R1 流水线最上游的「数据工厂」：它不训练模型，而是用 Distilabel 把 prompt 喂给推理模型、收集带思维链的回答。
- `build_distilabel_pipeline` 用**代码**（而非 YAML）装配出一条「只有一步 `TextGeneration`」的流水线，所有旋钮都做成函数入参，便于 CLI / Slurm 注入。
- `Pipeline().ray()` 让流水线**强制走 Ray 后端**——这是 open-r1 与 README smol 示例（进程内 vLLM、不起 Ray）在规模化上的根本区别。
- `TextGeneration` 通过 `OpenAILLM`（`api_key` 占位、`base_url` 指向 vLLM）连本地 vLLM 的 OpenAI 兼容服务；`input_mappings` 负责把数据集列改名为 `instruction`，`template` 负责渲染。
- `num_generations` + `group_generations=True` 决定「每条 prompt 生成多份回答并打包成列表」，输出行数等于输入行数 \(N\)（否则为 \(N \times G\)）；`StepResources(replicas=...)` 控制并行副本。
- `generate.py` 自身也是 CLI：`argparse` 解析参数 → `load_dataset` → `build_distilabel_pipeline` → `pipeline.run(..., use_cache=False)` → 可选 `distiset.push_to_hub`；在集群上则由 `slurm/generate.slurm` 把 vLLM 服务与 `generate.py` 分别作为两个 Ray job 提交。

## 7. 下一步学习建议

- **紧接的下一讲是 [u4-l2 数据去污染（Decontamination）](u4-l2-decontamination.md)**：生成出来的数据在拿去训练/评估前，必须剔除与基准测试集（MATH-500、AIME、GPQA 等）雷同的样本，否则评估分数会虚高。`scripts/decontaminate.py` 用 8-gram 检测这类污染，正好接在本讲「生成完数据」之后。
- **想了解生成出来的数据怎么进训练**：回到 [u2-l1 SFT 训练脚本主流程](u2-l1-sft-script-walkthrough.md) 与 [u2-l2 数据集加载与混合](u2-l2-dataset-loading.md)，看 `get_dataset` 如何把数据（含本讲产出的推理列）喂进 `SFTTrainer`。
- **想深入 distilabel 本身**：可阅读 distilabel 官方文档对 `Pipeline`、`StepResources`、`group_generations` 的说明，并对比 `OpenAILLM`（连服务）与 `vLLM`（进程内）两种 LLM 后端的取舍。
- **想看大规模怎么跑**：细读 [slurm/generate.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/generate.slurm) 里「Ray 集群 → vLLM 服务 → generate.py」三段式提交的细节，为后续 [u7-1 Slurm 集群训练与 vLLM 服务](u7-l1-slurm-vllm-training.md) 做铺垫。
