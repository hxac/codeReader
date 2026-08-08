# 基准评测框架：数据集与 CLI

## 1. 本讲目标

前三讲（u3-l1～u3-l3）我们一直在「算法内核」里打转——草稿模型怎么搭、注意力怎么拼、被拒 token 怎么回滚。但一个加速算法到底有没有用，最终要靠**评测**说话：在统一的数据集上、用统一的口径，量出 DFlash 相对 baseline 快了多少、接受长度有多高。`dflash/benchmark.py` 就是干这件事的模块。

本讲是评测主题的**第一讲**，只聚焦评测框架的「地基」——**数据从哪来、怎么缓存、CLI 怎么把请求分发到不同后端**。我们把四个后端运行器（`_run_transformers` / `_run_server` / `_run_mlx`）的**内部细节**留给下一讲 u3-l5，本讲只在「调用入口」这一层停下。学完本讲，你应该能够：

1. 读懂 [`DATASETS`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L28-L55) 这张配置表，说清每个数据集的 `load_args` / `load_kwargs` / `format` / `multi_turn` 四个字段各自的作用，并理解「声明式」描述数据集的好处。
2. 理解 [`_prepare_dataset`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L58-L81) 与 [`load_and_process_dataset`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L84-L93) 的「下载 + JSONL 原子缓存」机制——尤其是为什么要「先写临时文件、再 `os.replace`」，以及为什么所有数据集都被归一成 `{"turns": [...]}` 这一种格式。
3. 看懂 [`main`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L480-L520) 里的 `argparse` 参数分组，以及它如何按 `--backend` 把控制权分发给「库型后端」（transformers / mlx，需要 `--draft-model`）和「服务型后端」（vllm / sglang，连 HTTP 服务）两条截然不同的路径。

> 一句话定位：本讲是 u3-l5《多后端评测运行器与指标》的前置——先把「数据与入口」讲透，下一讲再讲「运行器内部与加速比指标」。

---

## 2. 前置知识

本讲假设你已经读过：

- **u1-l4**：跑通过一次 DFlash 生成，知道 target / draft 怎么配合。本讲的评测代码就是把那套生成循环套在成百上千条样本上跑。
- **u1-l2**：DFlash 的四种后端。本讲的 CLI 分发正是按「服务型（vllm / sglang）」与「库型（transformers / mlx）」两类来走的——这一点和 u1-l2 的后端分类完全对应。

下面三个本讲才出现的概念，用通俗语言补齐：

### 2.1 `datasets` 库与 `load_dataset`

Hugging Face 的 `datasets` 库是获取公开数据集的事实标准。它的核心 API 是 `load_dataset`：

```python
from datasets import load_dataset
ds = load_dataset("openai/gsm8k", "main", split="test")
# ds 是一个可迭代的 Dataset，每个 row 是一个 dict，如 {"question": "...", "answer": "..."}
```

第一个位置参数是仓库 ID（`"openai/gsm8k"`），第二个可选参数是子集名 / 配置名（`"main"`），`split=` 指定切分（`"test"` / `"train"`）。本讲的 `DATASETS` 表，本质上就是把这三个东西（位置参数、子集、split）外加「怎么把一行原始数据格式化成 prompt」一起存起来。

> 注意：`from datasets import load_dataset` 在本讲是被**延迟导入**的（写在 `_prepare_dataset` 函数内部，见 [L59](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L59)）。`datasets` 是个很重的依赖，只有真正需要下载时才 import，避免「只想跑 server 后端」的用户被迫装上它。这个模式我们在 u1-l3 的懒加载导出里见过同类思想。

### 2.2 原子写：临时文件 + `os.replace`

「原子（atomic）」这个词在并发编程里指「**要么完整完成、要么完全没发生，不会停留在中间态**」。文件写入天然不原子——如果进程在写到一半时崩溃，磁盘上就会留下一个**半截的、损坏的**文件。

标准做法是**两步走**：

1. 先把全部内容写到一个**临时文件**（比如 `gsm8k.jsonl.12345.tmp`，文件名带进程号防撞车）。
2. 全部写完后，用 `os.replace(tmp, dst)` 把临时文件**改名**成目标文件。

`os.replace` 在 POSIX 系统上是「重命名」系统调用，对同一文件系统内的改名是**原子**的——目标路径要么还是旧内容、要么瞬间变成完整的新内容，绝不会出现「半个新文件」。本讲的缓存写法就是这个套路。

### 2.3 `argparse`：Python 标准库的命令行解析

`argparse` 是 Python 内置的命令行参数解析库。基本用法：

```python
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--backend", choices=["vllm", "sglang"], required=True)
args = parser.parse_args()   # args.backend 就是用户传进来的字符串
```

`choices=` 限定取值范围；`required=True` 表示必填；`type=int/float/str` 做类型转换；`default=` 给默认值；`action="store_true"` 表示「出现这个 flag 就为 True、不出现就为 False」（适合 `--enable-thinking` 这种开关）。本讲的 [`main`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L480-L520) 用它定义了十几个参数。

---

## 3. 本讲源码地图

本讲只涉及一个文件，但只读它的「数据 + 入口」这一段，运行器内部留给 u3-l5：

| 文件 | 本讲关注的内容 | 运行器内部（u3-l5） |
|---|---|---|
| [`dflash/benchmark.py`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py) | `CACHE_DIR`（L26）、`DATASETS`（L28-L55）、`_prepare_dataset` / `load_and_process_dataset` / `_limit_dataset`（L58-L100）、`main` 的 argparse + 分发（L480-L520） | `_run_transformers`、`_run_server`、`_run_mlx`、`_print_decode_summary`（这些本讲只在「被分发」这一层提及） |

> 辅助常量：[`CACHE_DIR = Path(__file__).parent.parent / "cache"`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L26)。`__file__` 是 `dflash/benchmark.py`，`.parent` 是 `dflash/`，`.parent.parent` 是**项目根目录**，所以缓存目录是 `<项目根>/cache/`，与 `benchmark.py` 同级。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1** `DATASETS` 配置表：数据集的「声明式」描述。
2. **4.2** 数据集下载与 JSONL 原子缓存：`_prepare_dataset` / `load_and_process_dataset`。
3. **4.3** `main`：argparse 参数与后端分发。

---

### 4.1 `DATASETS` 配置表：数据集的「声明式」描述

#### 4.1.1 概念说明

评测要在「同样的题」上比，所以第一步是**确定数据集**。DFlash 选了五个公开 benchmark：`gsm8k`、`math500`（数学推理）、`humaneval`、`mbpp`（代码生成）、`mt-bench`（多轮对话）。这五个覆盖了 LLM 评测最常见的几类能力。

但光有「数据集名字」还不够——同一个数据集在 Hugging Face 上可能对应不同的仓库、不同的子集、不同的切分；而且**原始数据格式千差万别**（gsm8k 的题目在 `question` 字段，math500 在 `problem` 字段，humaneval 在 `prompt` 字段……）。要把它们都变成「**发给模型的 prompt 文本**」，必须各自做一次格式化。

`DATASETS` 用一种**声明式（declarative）**的写法解决了这个问题：把「怎么取数据」和「怎么格式化」**作为数据存进一张表**，而不是写五个独立的函数。每个数据集对应表里的一条记录，记录里写清楚四件事：

| 字段 | 作用 | 例子（gsm8k） |
|---|---|---|
| `load_args` | 传给 `load_dataset` 的位置参数（仓库 ID + 可选子集） | `("openai/gsm8k", "main")` |
| `load_kwargs` | 传给 `load_dataset` 的关键字参数（通常是 `split`） | `{"split": "test"}` |
| `format` | 一个 lambda，把**一行原始数据**格式化成 **prompt 字符串**（多轮时是字符串列表） | `lambda x: "{question}\n...\\boxed{}."` |
| `multi_turn` | 可选布尔，标记是否多轮对话（默认 `False`） | 无（只有 mt-bench 是 `True`） |

「声明式」的好处是：**加一个新数据集，只要往表里加一条记录，不用改任何函数逻辑**。下载、缓存、迭代的全流程都由 4.2 的通用函数驱动，对具体数据集「无感」。

#### 4.1.2 核心流程：从配置表到 prompt

把 `DATASETS` 想成一张「菜谱」，每一行是一道菜的用料和做法：

```
DATASETS["gsm8k"] = {
    "load_args":   ("openai/gsm8k", "main"),     # 去哪个仓库、取哪个子集
    "load_kwargs": {"split": "test"},            # 取哪个切分
    "format":      lambda x: "...",              # 拿到一行后怎么加工成 prompt
}
```

使用时（4.2 详述）的流程：

```
cfg = DATASETS["gsm8k"]
dataset = load_dataset(*cfg["load_args"], **cfg["load_kwargs"])   # 1. 取数据
for row in dataset:
    prompt = cfg["format"](row)                                   # 2. 格式化成 prompt
    # prompt 就是最终喂给模型的文本
```

关键点：`*load_args` 和 `**load_kwargs` 的解包语法，让配置表里的 tuple / dict **直接映射**成 `load_dataset` 的调用参数，零中间转换。

#### 4.1.3 源码精读

整张表定义在 [`dflash/benchmark.py:28-55`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L28-L55)。我们逐条看。

**gsm8k**（[L29-L33](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L29-L33)）—— GSM8K 是小学应用题数据集：

```python
"gsm8k": {
    "load_args": ("openai/gsm8k", "main"),
    "load_kwargs": {"split": "test"},
    "format": lambda x: "{question}\nPlease reason step by step, and put your final answer within \\boxed{{}}.".format(**x),
},
```

这里的 `format` lambda 有个**双层转义**的小机关，值得拆开看：

- `"{question}"` 是 `str.format` 的字段，会被 `x["question"]` 替换。
- `"\\boxed"` 在 Python 源码里 `\\` 是转义的反斜杠，字符串实际值是一个反斜杠 `\`，所以是 `\boxed`。
- `"{{}}"` 在 `str.format` 里，`{{` 转义成字面 `{`、`}}` 转义成字面 `}`，所以 `{{}}` → `{}`。

合起来，渲染出的 prompt 是：

```
<题目内容>
Please reason step by step, and put your final answer within \boxed{}.
```

`\boxed{}` 是 LaTeX 里「把答案框起来」的写法——这条指令要求模型把最终答案放进盒子里，方便后续用正则自动提取判分。math500（[L34-L38](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L34-L38)）用的是**完全相同**的 boxed 指令，只是取题字段从 `{question}` 换成了 `{problem}`。

**humaneval**（[L39-L43](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L39-L43)）—— 代码生成，把函数签名 `{prompt}` 包进一个 python 代码块：

```python
"format": lambda x: "Write a solution to the following problem and make sure that it passes the tests:\n```python\n{prompt}\n```".format(**x),
```

**mbpp**（[L44-L48](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L44-L48)）—— 最朴素，`format` 直接原样返回 `x["prompt"]`，不加任何指令前缀。

**mt-bench**（[L49-L54](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L49-L54)）—— 唯一的多轮数据集，多了一个字段：

```python
"mt-bench": {
    "load_args": ("HuggingFaceH4/mt_bench_prompts",),
    "load_kwargs": {"split": "train"},
    "format": lambda x: x["prompt"],
    "multi_turn": True,
},
```

注意 `multi_turn: True` 改变了 `format` 的**返回类型约定**：单轮数据集的 `format` 返回**一个字符串**；多轮数据集的 `format` 返回**一个字符串列表**（mt-bench 的 `x["prompt"]` 本身就是多轮提问的列表）。这个区别会在 4.2 的 `_prepare_dataset` 里被 `multi_turn` 标志分流处理——见下文。

> 小结：五条记录共享同一套字段结构，差异只在「取哪个仓库 / 哪个字段 / 加什么指令 / 是否多轮」。这就是声明式配置的威力。

#### 4.1.4 代码实践

**目标**：在不下载任何数据集的前提下，亲手渲染一条 gsm8k 的 prompt，看清 `format` lambda 到底产出什么文本。

**步骤**：

1. 在项目根目录启动 Python（需已装 `dflash`，见 u1-l3）。
2. 执行下面这段**示例代码**（非项目原有，为本讲编写）：

```python
# 示例代码：手动调用 DATASETS["gsm8k"] 的 format lambda
from dflash.benchmark import DATASETS

fake_row = {"question": "Janet's ducks lay 16 eggs per day. How many does she have left after selling?", "answer": "..."}
fmt = DATASETS["gsm8k"]["format"]
print(fmt(fake_row))
```

3. 再循环打印五个数据集各自的字段结构，确认它们都长得一样：

```python
# 示例代码
for name, cfg in DATASETS.items():
    print(name, "->", list(cfg.keys()), "| multi_turn =", cfg.get("multi_turn", False))
```

**需要观察的现象**：

- 第一段打印出题目正文 + `\boxed{}` 指令，注意 `\boxed{}` 里的反斜杠和大括号都正确保留（没有变成乱码或被吞掉）。
- 第二段确认五个数据集都有 `load_args` / `load_kwargs` / `format` 三个字段，只有 `mt-bench` 多出 `multi_turn = True`。

**预期结果**：

```
Janet's ducks lay 16 eggs per day. ...
Please reason step by step, and put your final answer within \boxed{}.

gsm8k     -> ['load_args', 'load_kwargs', 'format']            | multi_turn = False
math500   -> ['load_args', 'load_kwargs', 'format']            | multi_turn = False
humaneval -> ['load_args', 'load_kwargs', 'format']            | multi_turn = False
mbpp      -> ['load_args', 'load_kwargs', 'format']            | multi_turn = False
mt-bench  -> ['load_args', 'load_kwargs', 'format', 'multi_turn'] | multi_turn = True
```

> 这一步**不需要联网**，只是调用内存里的 lambda，所以能离线完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 gsm8k 的 `format` 里要写 `\\boxed{{}}` 而不是直接写 `\boxed{}`？

**参考答案**：因为这里用的是 `str.format(**x)` 而不是 f-string。在 `str.format` 里，单个 `{` / `}` 是字段定界符，要表示字面的大括号必须写成 `{{` / `}}`；而反斜杠在 Python 字符串字面量里本身就要 `\\` 转义成单个 `\`。所以 `\\boxed{{}}` 经「字符串字面量转义」+「format 转义」两层后，得到字面文本 `\boxed{}`。

**练习 2**：如果要加一个新数据集，最小改动是什么？

**参考答案**：只需在 `DATASETS` 字典里加一条记录，填好 `load_args`（仓库 + 子集）、`load_kwargs`（split）、`format`（一行数据 → prompt 的 lambda），可选 `multi_turn`。**不需要**改 `_prepare_dataset` / `load_and_process_dataset` / `main` 里的任何代码——它们都是按配置表通用驱动的。

---

### 4.2 数据集下载与 JSONL 原子缓存

#### 4.2.1 概念说明

`DATASETS` 表只是「配方」，真正把数据拿到手、存下来、供运行器反复读取的，是 [`_prepare_dataset`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L58-L81) 和 [`load_and_process_dataset`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L84-L93) 这一对函数。它们解决三个问题：

1. **何时下载**：第一次用某数据集时下载，之后直接读本地缓存——避免每次跑评测都重新下载几百 MB。
2. **怎么存**：无论原始数据集是什么格式（parquet、json、arrow……），统一存成 **JSONL**（每行一个 JSON 对象）。
3. **存成什么结构**：所有数据集都被归一成**同一种**结构 `{"turns": [prompt1, prompt2, ...]}`，让下游运行器只面对一种输入。

这里有两个关键设计需要先建立直觉：

- **统一结构 `{"turns": [...]}`**：`turns` 是「这一题的多轮提问列表」。单轮数据集（gsm8k 等）的 `turns` 只有一个元素；多轮数据集（mt-bench）有多个。这样运行器只需写一套循环：「对 `turns` 里的每一条提问，跑一轮生成」，就能同时处理单轮和多轮——不必为 mt-bench 单独写逻辑。
- **原子写**：如 2.2 节所述，下载结果先写临时文件，再 `os.replace` 改名。这样即使下载中途崩溃（网络中断、OOM），也**不会**留下一个半截的 `gsm8k.jsonl` 污染缓存——下次重跑时 `load_and_process_dataset` 会因为目标文件不存在而重新下载，不会误读到损坏文件。

#### 4.2.2 核心流程

下载并缓存一次数据集的完整流程（以 gsm8k 为例）：

```
load_and_process_dataset("gsm8k")
  │
  ├─ name 在 DATASETS 里？ 否 → raise ValueError
  ├─ cache/gsm8k.jsonl 存在？
  │     ├─ 是 → 直接读所有行，返回 list[dict]            # 命中缓存，秒回
  │     └─ 否 → _prepare_dataset("gsm8k")
  │               │
  │               ├─ CACHE_DIR.mkdir(exist_ok=True)
  │               ├─ out_path = cache/gsm8k.jsonl
  │               ├─ tmp_path = cache/gsm8k.jsonl.<pid>.tmp   # 带进程号的临时文件
  │               ├─ dataset = load_dataset(*load_args, **load_kwargs)   # 真正联网下载
  │               ├─ for row in dataset:
  │               │      if multi_turn: turns = format(row)        # format 返回 list
  │               │      else:        turns = [format(row)]        # format 返回 str，包成 list
  │               │      写一行 {"turns": turns} 到 tmp_path
  │               ├─ os.replace(tmp_path, out_path)   # 原子改名
  │               └─ 统计行数并打印
  └─ 读 cache/gsm8k.jsonl 所有行 → 返回 list[dict]
```

核心不变量：**返回值永远是 `list[dict]`，每个 dict 形如 `{"turns": [...]}`**。下游运行器（u3-l5）完全不需要知道这是 gsm8k 还是 mt-bench，只管遍历 `turns`。

#### 4.2.3 源码精读

**`_prepare_dataset`**（[L58-L81](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L58-L81)）—— 真正干活的下载函数：

```python
def _prepare_dataset(name: str) -> Path:
    from datasets import load_dataset          # 延迟导入，见 2.1

    cfg = DATASETS[name]
    CACHE_DIR.mkdir(exist_ok=True)
    out_path = CACHE_DIR / f"{name}.jsonl"
    tmp_path = out_path.with_name(f"{out_path.name}.{os.getpid()}.tmp")
```

- `os.getpid()` 把当前进程号塞进临时文件名。这一步对 **Transformers 分布式评测**（`torchrun` 起多个 rank，见 u3-l5）很关键：多个进程可能同时第一次下载同一数据集，带 pid 的临时文件名保证它们**各写各的临时文件、互不覆盖**，最后只有一个 `os.replace` 抢到目标名（其余的 `os.replace` 也会成功覆盖，但因为内容相同，结果一致）。

接着下载并逐行写：

```python
    dataset = load_dataset(*cfg["load_args"], **cfg["load_kwargs"])

    with open(tmp_path, "w") as f:
        for row in dataset:
            if cfg.get("multi_turn"):
                turns = cfg["format"](row)        # format 返回 list（mt-bench）
            else:
                turns = [cfg["format"](row)]      # format 返回 str，包成单元素 list
            f.write(json.dumps({"turns": turns}) + "\n")
    os.replace(tmp_path, out_path)                # 原子改名（见 2.2）
```

这里正是 4.1 提到的「`multi_turn` 改变 format 返回类型约定」的分流点：

- 单轮：`format(row)` 返回**字符串**，用 `[...]` 包成 `["单条prompt"]`。
- 多轮：`format(row)` 已经返回**列表**（如 `["第一轮提问", "第二轮提问"]`），直接用。

两种情况最后都写成 `{"turns": [...]}`，结构完全一致。最后 `os.replace` 把临时文件原子地改名成正式缓存文件。

**`load_and_process_dataset`**（[L84-L93](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L84-L93)）—— 对外的「带缓存的读取」入口（也是 u1-l3 提到的四个公开 API 之一）：

```python
def load_and_process_dataset(data_name: str) -> list[dict]:
    if data_name not in DATASETS:
        raise ValueError(f"Unknown dataset '{data_name}'. Available: {list(DATASETS.keys())}")

    path = CACHE_DIR / f"{data_name}.jsonl"
    if not path.exists():
        _prepare_dataset(data_name)              # 缓存不存在才下载

    with open(path) as f:
        return [json.loads(line) for line in f]  # 读所有行
```

逻辑极简：**先查缓存文件在不在，不在才下载**；在就直接读。这就是「第一次慢、之后秒回」的缓存策略。注意它返回的是 `list[dict]`，已经在内存里，下游可以随机访问、切片、shuffle。

**`_limit_dataset`**（[L96-L100](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L96-L100)）—— 配套的「抽样」小工具：

```python
def _limit_dataset(dataset: list[dict], max_samples: int | None) -> list[dict]:
    if max_samples is None or len(dataset) <= max_samples:
        return dataset
    random.shuffle(dataset)
    return dataset[:max_samples]
```

当 `--max-samples N` 时，先 `shuffle` 再取前 N 条。注意它**原地 shuffle** 了传入的 list（`random.shuffle` 是原地操作），且依赖模块顶部 [L23](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L23) 的 `random.seed(42)`（不过 Transformers 运行器会在 [L207](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L207) 把种子重置为 0）。

> 串联起来：`load_and_process_dataset`（拿全量）→ `_limit_dataset`（按需抽样）是所有运行器获取数据的统一两步。具体每个运行器怎么用，见 u3-l5。

#### 4.2.4 代码实践

**目标**：亲手触发一次 gsm8k 的下载与缓存，观察 `cache/` 目录的产物，并验证缓存命中后的「秒回」效果。

**步骤**：

1. 删除已有缓存（如果之前跑过）：`rm -f cache/gsm8k.jsonl`（`cache/` 在项目根目录）。
2. 在 Python 里执行（**需要联网**，且已装 `datasets`）：

```python
from dflash.benchmark import load_and_process_dataset
data = load_and_process_dataset("gsm8k")
print("样本数:", len(data))
print("第一条结构:", type(data[0]), list(data[0].keys()))
print("第一条 turns[0] 前 80 字:", data[0]["turns"][0][:80])
```

3. 在 shell 里检查缓存文件：

```bash
ls -la cache/
head -c 300 cache/gsm8k.jsonl
wc -l cache/gsm8k.jsonl
```

4. **再跑一次**第 2 步的 `load_and_process_dataset("gsm8k")`。

**需要观察的现象**：

- 第 2 步首次运行时打印 `[download] gsm8k ...`，耗时较长（联网下载）。
- 第 3 步看到 `cache/gsm8k.jsonl` 存在，每行是一个 `{"turns": ["..."]}` 的 JSON，行数等于 gsm8k test 集大小（1319 题）。
- 第 4 步再次运行时**不再出现 `[download]`**，几乎瞬间返回——证明命中了本地缓存。

**预期结果**：

- `样本数: 1319`（gsm8k test 切分大小）。
- `第一条结构: <class 'dict'> ['turns']`——注意顶层只有 `turns` 一个 key，与 4.2.1 的统一结构吻合。
- `turns[0]` 以题目内容开头，结尾是 `...within \boxed{}.`。
- `wc -l` 输出 1319。

> 若无网络或未装 `datasets`，第 2 步会失败——这种情况请改为「源码阅读型实践」：直接读 [`_prepare_dataset`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L58-L81) 源码，在纸上追踪 `multi_turn` 分支，标注待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：假设下载 gsm8k 时进程在写临时文件写到一半被 `kill -9`，会发生什么？下次再跑会怎样？

**参考答案**：磁盘上会留下一个 `cache/gsm8k.jsonl.<pid>.tmp` 的半截临时文件，但**正式的 `cache/gsm8k.jsonl` 不会存在**（因为还没走到 `os.replace`）。下次再跑 `load_and_process_dataset("gsm8k")` 时，`path.exists()` 为 `False`，于是重新调用 `_prepare_dataset` 重新下载——那个残留的 `.tmp` 文件不会污染结果（新的临时文件带新的 pid）。最坏情况只是磁盘上多了个垃圾 tmp 文件，需要手动清理。

**练习 2**：为什么 `turns` 设计成列表，而不是直接存一个 prompt 字符串？

**参考答案**：为了用**同一套数据结构和同一套运行器循环**同时支持单轮和多轮数据集。单轮数据集的 `turns` 是 `[prompt]`（长度 1），多轮（mt-bench）的 `turns` 是 `[turn1, turn2, ...]`。运行器只需 `for user_content in instance["turns"]:` 就能通用处理，不用为 mt-bench 写单独的多轮逻辑。

**练习 3**：`_prepare_dataset` 里临时文件名为什么要带 `os.getpid()`？

**参考答案**：防止多进程并发下载时互相覆盖临时文件。最典型的场景是 `torchrun --nproc_per_node=N` 起的分布式 Transformers 评测（u3-l5）：N 个 rank 进程可能同时第一次下载同一数据集，各自写 `gsm8k.jsonl.<各自pid>.tmp`，互不干扰。

---

### 4.3 `main`：argparse 参数与后端分发

#### 4.3.1 概念说明

有了数据，还差一个「**入口**」把「用哪个后端、跑哪个模型、测哪个数据集」这些选择串起来。这就是 [`main`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L480-L520) 函数——它通过 `python -m dflash.benchmark ...` 被调用，是整个评测 CLI 的总控。

`main` 的核心职责只有两件：

1. **解析命令行参数**：用 `argparse` 把十几条 flag 解析成一个 `args` 命名空间。
2. **按后端分发**：根据 `--backend` 把 `args` 交给对应的运行器函数。

理解分发的关键是 u1-l2 建立的「两类后端」心智模型：

| 后端类别 | 成员 | 工作方式 | 是否要 `--draft-model` | 抽样/规模参数 |
|---|---|---|---|---|
| **库型** | transformers、mlx | 在当前进程里 import 并加载 target + draft，直接跑生成 | **要**（进程内加载草稿） | `--max-samples`（抽 N 条，每条跑 baseline+DFlash 对比） |
| **服务型** | vllm、sglang | 连一个**已经在跑**的 HTTP 服务（`--base-url`），发请求测吞吐 | **不要**（草稿已在服务端配好） | `--num-prompts` / `--concurrency`（并发压测） |

`main` 的分发逻辑严格对应这张表：库型后端会**强制检查** `--draft-model` 是否提供（没有就 `parser.error` 报错退出），服务型后端则直接放行。参数也因此分成三组——通用参数、库型专用、服务型专用——它们共存于同一个 `argparse` 里，由各自的运行器按需取用。

#### 4.3.2 核心流程

`main` 的执行流程：

```
main()
  ├─ 构建 argparse，定义 ~14 个参数（带默认值/choices）
  ├─ args = parser.parse_args()
  ├─ 断言：--enable-thinking 不能配 qwen3-4b / qwen3-8b 草稿（未用 thinking 数据训练）
  └─ 按 args.backend 分发：
        transformers → 检查 --draft-model → _run_transformers(args)
        mlx          → 检查 --draft-model → _run_mlx(args)
        vllm/sglang  → _run_server(args)            # 草稿已在服务端，无需 --draft-model
```

三个运行器（`_run_transformers` / `_run_mlx` / `_run_server`）的**内部细节**是 u3-l5 的主题。本讲只关注 `main` 如何定义参数、如何选择路径——也就是「入口层」。

#### 4.3.3 源码精读

**参数定义**（[L481-L498](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L481-L498)）。按用途分组来看：

```python
parser = argparse.ArgumentParser(description="DFlash benchmark")
# —— 通用参数（所有后端都用）——
parser.add_argument("--backend", choices=["transformers", "sglang", "vllm", "mlx"], required=True)
parser.add_argument("--model", type=str, required=True)
parser.add_argument("--dataset", type=str, required=True)
parser.add_argument("--max-new-tokens", type=int, default=2048)
parser.add_argument("--temperature", type=float, default=0.0)

# —— 库型后端专用 ——
parser.add_argument("--draft-model", type=str, default=None)
parser.add_argument("--block-size", type=int, default=None)
parser.add_argument("--max-samples", type=int, default=None)

# —— 服务型后端专用 ——
parser.add_argument("--base-url", type=str, default="http://127.0.0.1:30000")
parser.add_argument("--num-prompts", type=int, default=1024)
parser.add_argument("--concurrency", type=int, default=1)
parser.add_argument("--top-p", type=float, default=1.0)
parser.add_argument("--top-k", type=int, default=1)
parser.add_argument("--enable-thinking", action="store_true")
parser.add_argument("--timeout-s", type=int, default=3600)
```

几个要点：

- `--backend` 用 `choices=` 限定四个合法值，传错会自动报错。
- `--temperature` 默认 `0.0`（贪心解码），呼应 u1-l4 / u2-l4 的结论——评测默认走贪心以保证可复现，升温会降低接受长度。
- `--block-size` 默认 `None`，运行器会回退到**草稿模型 config 里的 block_size**（如 `b16` → 16），见 `_run_transformers` 的 [L227](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L227) 与 `_run_mlx` 的 [L341](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L341)。
- 服务型默认 `--base-url` 指向 `127.0.0.1:30000`（SGLang 默认端口）；vLLM 通常用 8000，故 README 的 vLLM 示例显式传了 `--base-url http://127.0.0.1:8000`。
- 注意 `argparse` 的**自动命名转换**：命令行用 `--max-new-tokens`（连字符），但解析后访问要用 `args.max_new_tokens`（下划线）。

**前置断言**（[L502-L505](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L502-L505)）：

```python
assert not (args.enable_thinking and any(x in args.model.lower() for x in ["qwen3-4b", "qwen3-8b"])), (
    "DFlash draft models for Qwen3-4B and Qwen3-8B were not trained with thinking traces. "
    "Using --enable-thinking will lead to suboptimal performance."
)
```

这是一道**防呆**：Qwen3-4B / Qwen3-8B 的 DFlash 草稿模型训练时没用到 thinking 轨迹，强行开 `--enable-thinking` 会得到次优结果，所以提前拦下。它检查的是 `args.model`（目标模型名）里是否含 `qwen3-4b` / `qwen3-8b` 字样。

**后端分发**（[L507-L516](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L507-L516)）——本讲的核心：

```python
if args.backend == "transformers":
    if args.draft_model is None:
        parser.error("--draft-model is required for transformers backend")
    _run_transformers(args)
elif args.backend == "mlx":
    if args.draft_model is None:
        parser.error("--draft-model is required for mlx backend")
    _run_mlx(args)
else:
    _run_server(args)     # vllm / sglang
```

这段代码就是 4.3.1 那张表的直接翻译：

- **库型后端**（transformers / mlx）：草稿模型要在当前进程里加载（见 u2-l5 的 `DFlashDraftModel.from_pretrained`、u3-l1 的 `load_draft`），所以**强制要求** `--draft-model`，缺失就用 `parser.error` 报错退出（`parser.error` 会打印信息并以非零码退出，比裸 `assert` 更友好）。
- **服务型后端**（vllm / sglang）：草稿模型已经在**服务端**通过 `--speculative-config` / speculative flag 配好了（回顾 u1-l2），CLI 这边只需发 HTTP 请求，所以**不要求** `--draft-model`，直接进 `_run_server`。

最后，模块底部 [L519-L520](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L519-L520) 的 `if __name__ == "__main__": main()` 让 `python -m dflash.benchmark` 能直接当作 CLI 入口运行。

> 入口对比：README 给出了四种后端各自的调用命令（见本讲 4.3.4），它们的**差异全部来自 4.3.1 的那张表**——库型带 `--draft-model --max-samples`，服务型带 `--base-url --num-prompts --concurrency`。

#### 4.3.4 代码实践

**目标**：用 `--help` 把 `argparse` 定义的参数全貌打印出来，再对照 README 的四条命令，验证「库型 vs 服务型」的参数差异。

**步骤**：

1. 打印帮助（**不需要联网、不需要 GPU**，纯解析）：

```bash
python -m dflash.benchmark --help
```

2. 对照 README 的四条评测命令（[README L167-L193](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L167-L193)），把每条命令的参数归类到「通用 / 库型专用 / 服务型专用」三组。摘录两条对照：

   - **vLLM（服务型）**：`--backend vllm --base-url ... --model ... --dataset gsm8k --num-prompts 128 --concurrency 1 --enable-thinking`——**没有** `--draft-model`，有 `--base-url / --num-prompts / --concurrency`。
   - **Transformers（库型）**：`--backend transformers --model ... --draft-model ... --dataset gsm8k --max-samples 128`——**有** `--draft-model / --max-samples`，**没有** `--base-url / --num-prompts`。

3.（可选，需对应环境）故意省略 `--draft-model` 跑库型后端，观察防呆报错：

```bash
python -m dflash.benchmark --backend mlx --model some/model --dataset gsm8k
```

**需要观察的现象**：

- 第 1 步的 `--help` 列出全部 ~14 个参数及其默认值，`--backend` 标注 `{transformers,sglang,vllm,mlx}`。
- 第 2 步确认：服务型命令普遍有 `--base-url`、`--num-prompts`、`--concurrency`，没有 `--draft-model`；库型命令相反。
- 第 3 步出现 `--draft-model is required for mlx backend` 并退出（非 0 退出码）。

**预期结果**：

- `--help` 输出里能找到 `--max-samples`（默认 `None`）、`--num-prompts`（默认 `1024`）、`--concurrency`（默认 `1`）、`--base-url`（默认 `http://127.0.0.1:30000`）等。
- 第 3 步的报错文案与源码 [L513](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L513) 完全一致。

> 若本机没有对应的 Python 环境，第 1、3 步可改为直接阅读 [`main` 源码 L480-L520](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L480-L520)，在笔记里手写「参数 → 归属后端类别」的映射表，标注待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么服务型后端（vllm / sglang）的 CLI 不需要 `--draft-model`，而库型后端必须要？

**参考答案**：服务型后端把草稿模型配置放在了**服务端启动参数**里（vLLM 的 `--speculative-config`、SGLang 的 speculative flag，见 u1-l2），CLI 这边只对已起好的 HTTP 服务发请求做压测，草稿早已就位，所以不传。库型后端在**当前进程内**加载并驱动草稿模型（`from_pretrained` / `load_draft`），必须由 CLI 显式给出草稿路径，缺失就 `parser.error` 退出。

**练习 2**：`--max-samples`（库型）和 `--num-prompts`（服务型）都用来「控制评测规模」，它们的语义有何不同？

**参考答案**：`--max-samples` 是对**数据集抽样**——从数据集里随机选 N 条，每条各跑一次 baseline（block_size=1）和 DFlash，用于在进程内**对比每条样本的延迟/加速比**。`--num-prompts` 是**发送的请求数**——通过 `i % len(dataset)` 循环复用数据集凑够 N 条请求，配合 `--concurrency` 做并发**吞吐压测**。前者关心「单条快多少倍」，后者关心「整体每秒多少 token」。（两者具体实现见 u3-l5。）

**练习 3**：`--enable-thinking` 配合 Qwen3-8B 目标模型时为什么会被断言拦下？

**参考答案**：见 [L502-L505](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L502-L505) 的注释——Qwen3-4B / Qwen3-8B 的 DFlash 草稿模型**训练时没有用到 thinking 轨迹**，强行开启 thinking 会和草稿的训练分布不匹配，导致接受长度下降、加速效果变差，所以用 `assert` 提前拦截。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「**给 benchmark 增加一个新数据源**」的小任务。这个任务会逼你同时用到 `DATASETS` 配置（4.1）、缓存机制（4.2）和 CLI 入口（4.3）的全部知识。

**任务背景**：假设你想用 [MBPP](https://huggingface.co/google-research-datasets/mbpp) 的**完整版**（而非 `sanitized` 子集）来评测代码生成。`sanitized` 已经在 `DATASETS` 里（[L44-L48](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L44-L48)），现在要新增一个 `mbpp-full`。

**操作步骤**：

1. **写配置**。在 [`DATASETS`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L28-L55) 字典里新增一条（这是本任务唯一需要改源码的地方——注意：本讲强调「只读源码」，实际动手时请在自己的 fork / 分支上改，不要污染原仓库）：

   ```python
   "mbpp-full": {
       "load_args": ("google-research-datasets/mbpp",),   # 不传子集，取完整版
       "load_kwargs": {"split": "test"},
       "format": lambda x: x["prompt"],                    # 复用 mbpp 的格式化逻辑
   },
   ```

   思考题（先别看答案）：为什么这里不写 `multi_turn`？——因为它是单轮数据集，默认 `False`，`_prepare_dataset` 会把 `format` 返回的字符串包成 `[prompt]`。

2. **下载数据**。运行**示例代码**：

   ```python
   # 示例代码：单独触发新数据集的下载与缓存
   from dflash.benchmark import load_and_process_dataset
   data = load_and_process_dataset("mbpp-full")
   print("样本数:", len(data))
   print("首条 turns:", data[0]["turns"][0][:60])
   ```

3. **检查缓存**：

   ```bash
   ls cache/                 # 应能看到新生成的 mbpp-full.jsonl
   wc -l cache/mbpp-full.jsonl
   head -1 cache/mbpp-full.jsonl | python -m json.tool
   ```

4. **跑评测入口**（不需要真跑生成，只验证 CLI 能识别新数据集）。先确认 `--dataset mbpp-full` 能被接受——选一个你机器可用的后端，例如有本地 vLLM/SGLang 服务时：

   ```bash
   python -m dflash.benchmark --backend vllm --base-url http://127.0.0.1:8000 \
       --model Qwen/Qwen3-8B --dataset mbpp-full --num-prompts 4 --concurrency 1
   ```

   若无服务，则退化为「源码阅读型」：追踪 `main` → `_run_server`（[L380](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L380)）→ 内部调用 `load_and_process_dataset(args.dataset)`（[L382](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L382)），确认 `args.dataset == "mbpp-full"` 会正确命中你新增的配置、读到 `cache/mbpp-full.jsonl`。

**需要观察的现象**：

- 第 2 步首次运行打印 `[download] mbpp-full ...` 然后 `[cached] cache/mbpp-full.jsonl  (N samples)`；再次运行不再下载。
- 第 3 步 `cache/mbpp-full.jsonl` 每行都是 `{"turns": ["..."]}` 单 key 结构。
- 第 4 步（若实跑）评测能正常发起，不再报 `Unknown dataset`。

**预期结果**：

- `mbpp-full` 的 test 切分约 974 题（完整版，多于 sanitized 的约 427 题）；具体数字**待本地验证**（以 Hugging Face 上该数据集当前大小为准）。
- 新增一条配置后，`_prepare_dataset` / `load_and_process_dataset` / `main` 均**无需改动**即能处理新数据集——这正是 4.1「声明式配置」的价值。

> 这个综合实践把三个最小模块串成闭环：**写配置（4.1）→ 触发下载缓存（4.2）→ CLI 识别并分发（4.3）**。完成后你就掌握了给 DFlash 评测框架接入任意 Hugging Face 数据集的全部能力。

---

## 6. 本讲小结

- `DATASETS` 是一张**声明式**配置表，每个数据集用 `load_args` / `load_kwargs` / `format` / `multi_turn` 四个字段描述「去哪取、取哪部分、怎么格式化成 prompt、是否多轮」。加新数据集只需加一条记录，不动任何函数。
- 五个数据集（gsm8k / math500 / humaneval / mbpp / mt-bench）共享同一套字段结构；其中 gsm8k / math500 用 `\boxed{}` 指令要求模型把答案框起来，mt-bench 是唯一的多轮数据集（`multi_turn: True`，其 `format` 直接返回列表）。
- `_prepare_dataset` 用「**临时文件 + `os.replace`**」做**原子写**缓存：临时文件名带 `os.getpid()` 防止多进程（如 `torchrun`）互踩；即使下载崩溃也不会留下半截的正式缓存文件。
- 所有数据集被归一成同一种结构 `{"turns": [prompt, ...]}`，`load_and_process_dataset`（公开 API 之一）做「**缓存命中则读、否则下载**」，返回 `list[dict]`，让下游运行器只面对一种输入。
- `main` 用 `argparse` 定义约 14 个参数，分通用 / 库型专用（`--draft-model` / `--block-size` / `--max-samples`）/ 服务型专用（`--base-url` / `--num-prompts` / `--concurrency` …）三组。
- 分发严格对应「两类后端」：库型（transformers / mlx）**强制要求** `--draft-model` 并在进程内加载草稿；服务型（vllm / sglang）连 HTTP 服务、不传草稿，草稿已在服务端配好。运行器的**内部实现与加速比指标**是下一讲 u3-l5 的主题。

---

## 7. 下一步学习建议

本讲只打开了评测框架的「数据 + 入口」这一层，三个运行器内部仍是黑盒。建议接着学：

- **u3-l5《多后端评测运行器与指标》**：直接承接本讲。它会拆开 `_run_transformers`（含 `torchrun` 分布式、`_dist_init` / `_dist_gather`、按 rank 数据分片）、`_run_server`（`ThreadPoolExecutor` 并发 HTTP 压测、warmup、`_send_vllm` / `_send_sglang`）、`_run_mlx`，以及 `_print_decode_summary` 里**加速比**和**接受长度直方图**的计算方法——即 `--max-samples` 与 `--num-prompts` 各自背后到底怎么算指标。
- **回看 u1-l2**：如果你对「服务型后端的草稿配置」还有印象模糊，可重读 vLLM 的 `--speculative-config` 与 SGLang 的 speculative flag 部分，体会「服务端配草稿 / CLI 不传草稿」这条链路的两端。
- **动手扩展**：完成第 5 节的综合实践后，可以尝试为本地一个私有小数据集（自建 JSONL）写一个不走 `datasets` 的旁路加载函数，加深对 `{"turns": [...]}` 归一结构的理解。

> 读源码顺序建议：先固定本讲的 `main` 入口（L480-L520）作为「地图」，再带着「每个运行器怎么用 `load_and_process_dataset` 的返回值」这个问题进入 u3-l5。
