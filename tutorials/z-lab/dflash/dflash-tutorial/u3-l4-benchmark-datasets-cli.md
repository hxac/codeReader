# 基准评测框架：数据集与 CLI

## 1. 本讲目标

前面三讲（u3-l1、u3-l2、u3-l3）我们钻进了 MLX 后端的「推理引擎」内部，搞清楚了草稿模型、流式生成循环和混合模型的 GDN 状态回滚。本讲我们 **跳出生成引擎本身**，站到「怎么衡量 DFlash 到底快了多少」这一层：评测（benchmark）。

读完本讲，你应当能够：

1. 说出 `dflash/benchmark.py` 在整个项目里的定位：它是一份 **既能当库导入、又能当命令行跑** 的评测工具，把「下载数据集 → 格式化成 prompt → 喂给某个后端 → 算加速比」串成一条流水线。
2. 读懂 `DATASETS` 这张配置表：理解每条配置的 `load_args` / `load_kwargs` / `format` 三个字段分别做什么，以及为什么 `mt-bench` 多一个 `multi_turn` 字段。
3. 理解数据集「下载 + JSONL 原子写缓存」机制：为什么用 `tmp + os.replace` 而不是直接写目标文件。
4. 看懂 `main()` 里的 `argparse` 参数表，以及它如何按 `--backend` 把任务分发给 `_run_transformers` / `_run_mlx` / `_run_server` 三条路径。

本讲**只讲数据集与 CLI 这一侧**；三条后端运行器（分布式、并发 HTTP、MLX）的内部细节与加速比指标计算留给下一讲 u3-l5。

## 2. 前置知识

### 2.1 为什么需要单独的评测框架

投机解码（speculative decoding）的价值主张是「**不损失质量、单纯提速**」（见 u2-l4 的等价性证明）。要论证「提速」就必须测量，而测量生成速度有三个棘手之处：

- **数据要统一**：不能今天用 gsm8k、明天用自编问题，否则跨实验没法比较。所以要把常用数据集集中管理、格式化方式固定。
- **下载要可重复**：数据集从 Hugging Face Hub 拉，网络可能中断；不能每次评测都重新下载几百 MB。
- **入口要统一**：DFlash 有四种后端（Transformers / vLLM / SGLang / MLX），它们的调用方式完全不同（库调用 vs HTTP 服务），但评测流程相同，需要一层 CLI 把差异封住。

`benchmark.py` 就是来解决这三件事的。

### 2.2 你需要先记住的两个事实

在进入源码前，请先从前面讲义里回忆两点：

1. **DFlash 是「草稿 + 验证」**（u1-l1、u2-l1）。评测时通常要同时跑 `block_size=1`（即纯 target 自回归，作为 baseline）和 `block_size>1`（DFlash 加速），再对比两者吞吐，才能得到加速比。
2. **benchmark 的顶层公开 API 是 `load_and_process_dataset`**（u1-l3）。它被 `__init__.py` 的 `__all__` 收录，所以既可 `from dflash.benchmark import ...` 也可 `from dflash import ...`（经懒加载）。

### 2.3 几个 Python 小知识点

- **`argparse`**：Python 标准库的命令行解析器，`add_argument` 注册参数，`parse_args()` 得到一个带属性的 `Namespace` 对象（如 `args.model`）。
- **`os.replace(src, dst)`**：原子地把 `src` 重命名为 `dst`。即使在同一个文件系统内被信号打断，也只会出现「旧文件还在」或「新文件已就位」两种结果，**绝不会出现半个文件**——这是做缓存写入的关键原语。
- **Hugging Face `datasets` 库的 `load_dataset`**：第一个参数是仓库 id（如 `"openai/gsm8k"`），后续位置参数和关键字参数传给该仓库的 builder。返回一个可迭代的 `Dataset` 对象。

## 3. 本讲源码地图

本讲几乎全部围绕**一个文件**：

| 文件 | 作用 | 本讲关注的行段 |
| --- | --- | --- |
| [dflash/benchmark.py](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py) | 评测模块：数据集管理、CLI、后端分发 | `CACHE_DIR`、`DATASETS`、`_prepare_dataset`、`load_and_process_dataset`、`main` |

辅助参考（非本讲精读对象）：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md) | 评测章节给出四种后端的命令行示例 |
| [dflash/__init__.py](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/__init__.py) | `__all__` 把 `load_and_process_dataset` 暴露为顶层 API |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 `DATASETS` 配置表与 prompt 格式化** —— 五个数据集怎么描述。
- **4.2 数据集下载与 JSONL 原子写缓存** —— `_prepare_dataset` / `load_and_process_dataset`。
- **4.3 CLI 参数解析** —— `main` 的 `argparse`。
- **4.4 后端分发逻辑** —— `main` 如何按 `--backend` 路由。

### 4.1 DATASETS 配置表与 prompt 格式化

#### 4.1.1 概念说明

不同数据集长得完全不一样：gsm8k 每条是 `{"question": ..., "answer": ...}`，humaneval 每条是 `{"prompt": ..., "test": ...}`，MATH-500 是 `{"problem": ..., "solution": ...}`。如果把「下载哪个仓库、取哪个 split、把一行数据格式化成什么 prompt」散落在代码各处，维护会很痛苦。

`DATASETS` 用 **一张声明式配置表** 解决这个问题：每个数据集名映射到一个 dict，把上述三件事各放一个字段。要加新数据集，只需在表里加一行，不碰任何控制流。这是一种典型的「**数据驱动 / 表驱动**」设计。

#### 4.1.2 核心流程

每个数据集的配置由四个字段组成（最后一个可选）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `load_args` | 元组 | 传给 `datasets.load_dataset` 的位置参数，第一项通常是 Hugging Face 仓库 id |
| `load_kwargs` | dict | 传给 `load_dataset` 的关键字参数，通常是 `{"split": "test"}` |
| `format` | `lambda row -> str 或 list[str]` | 把**一条原始数据**格式化成 prompt 文本；`multi_turn` 为真时返回多轮列表 |
| `multi_turn` | bool（可选） | 是否多轮对话（仅 `mt-bench` 为 `True`） |

#### 4.1.3 源码精读

先看缓存目录与整张表（共支持 5 个数据集）：

[DATASETS 配置表 — dflash/benchmark.py:26-55](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L26-L55)

```python
CACHE_DIR = Path(__file__).parent.parent / "cache"

DATASETS = {
    "gsm8k": {
        "load_args": ("openai/gsm8k", "main"),
        "load_kwargs": {"split": "test"},
        "format": lambda x: "{question}\nPlease reason step by step, ...".format(**x),
    },
    "math500":   { "load_args": ("HuggingFaceH4/MATH-500",),         ... },
    "humaneval": { "load_args": ("openai/openai_humaneval",),        ... },
    "mbpp":      { "load_args": ("google-research-datasets/mbpp", "sanitized"), ... },
    "mt-bench": {
        "load_args": ("HuggingFaceH4/mt_bench_prompts",),
        "load_kwargs": {"split": "train"},
        "format": lambda x: x["prompt"],
        "multi_turn": True,
    },
}
```

几个要点：

1. **`CACHE_DIR` 在项目根目录的 `cache/`**，不在 `dflash/` 包里。`Path(__file__).parent.parent`：`__file__` 是 `dflash/benchmark.py`，`.parent` 是 `dflash/`，再 `.parent` 是项目根。这与 README「cached as JSONL in `cache/`」的说法一致。
2. **`load_args` 是元组，会被 `*` 解包**：`load_dataset(*cfg["load_args"], **cfg["load_kwargs"])`。所以 gsm8k 实际调用 `load_dataset("openai/gsm8k", "main", split="test")`，这里的 `"main"` 是 gsm8k 仓库的配置名（gsm8k 有 `main` 和 `socratic` 两个子集）。
3. **`format` 用 `.format(**x)` 解包原始行**：lambda 收到的 `x` 是数据集的一行字典，`**x` 把它展开成关键字参数。例如 gsm8k 的 `format` 用 `{question}` 取出问题、再拼一句「请逐步推理，最终答案放在 `\boxed{}` 里」。这就是 **prompt 模板与数据耦合在一条配置里**。
4. **`mt-bench` 的 `multi_turn: True`**：mt-bench 是多轮对话基准，它的 `format` 返回一个**字符串列表**（每个元素是一轮的 user 内容），其它数据集 `format` 返回单个字符串。这个布尔标志在 4.2 节会决定写缓存时如何处理。

#### 4.1.4 代码实践

**目标**：不下载、不联网，仅凭 Python 字符串演练 `format` 字段如何工作。

**步骤**：

1. 打开一个 Python 终端，把 gsm8k 的 `format` lambda 手抄出来。
2. 造一条假的 gsm8k 行，调用它，观察输出 prompt。

```python
# 示例代码（非项目原有代码，仅为演示 format 字段）
fmt = lambda x: "{question}\nPlease reason step by step, and put your final answer within \\boxed{{}}.".format(**x)
fake_row = {"question": "What is 2+2?", "answer": "4"}
print(fmt(fake_row))
```

**预期结果**：

```text
What is 2+2?
Please reason step by step, and put your final answer within \boxed{}.
```

注意：模板里的 `\\boxed{{}}` 在 `.format` 之后变成 `\boxed{}`——双花括号转义成单花括号，`\\` 转义成单个反斜杠。这是 Python `str.format` 的标准行为，理解它才能读懂表里的模板。

**待本地验证**：上面的输出需要你在本地确认实际字符串字面量。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mbpp` 和 `mt-bench` 的 `format` 是 `lambda x: x["prompt"]`，而 gsm8k 要拼一长串？

> **答案**：mbpp 与 mt-bench 数据集本身已经把 prompt 字段写成「可直接喂给模型」的成品文本；而 gsm8k/math500 原始字段只有题目，需要补一句答题格式指令（要求逐步推理、答案放 `\boxed{}`）来引导模型输出可解析的答案，方便后续评分。

**练习 2**：如果某个数据集仓库需要传 `trust_remote_code=True` 才能加载，应该改哪个字段？

> **答案**：加到 `load_kwargs`，例如 `"load_kwargs": {"split": "test", "trust_remote_code": True}`，因为它会被 `**cfg["load_kwargs"]` 解包成 `load_dataset` 的关键字参数。

---

### 4.2 数据集下载与 JSONL 原子写缓存

#### 4.2.1 概念说明

`DATASETS` 只是「描述」，真正去 Hugging Face 下载数据、落盘、再读回来的是 `_prepare_dataset` 和 `load_and_process_dataset` 这对函数。

这里有一个工程上很关键的设计：**原子写缓存（atomic write）**。下载 + 格式化可能耗时几十秒甚至几分钟，期间进程可能被中断（Ctrl-C、OOM、网络超时）。如果直接写到目标文件 `gsm8k.jsonl`，中断后就会留下一个**残缺的半截文件**；下次运行时程序一看「文件已存在」就跳过下载，于是永远用一个坏掉的缓存。

原子写的套路是：**先写到临时文件，全部写完后再用 `os.replace` 一次性改名到目标路径**。`os.replace` 在同一文件系统上是原子的——要么旧文件还在，要么新文件完整就位，不存在中间态。

#### 4.2.2 核心流程

`load_and_process_dataset` 是外部入口，`_prepare_dataset` 是内部下载器。流程如下：

```
load_and_process_dataset(name)
   │
   ├─ name 不在 DATASETS？→ raise ValueError（白名单校验）
   │
   ├─ 计算 cache/<name>.jsonl
   │
   ├─ 文件不存在？
   │     └─ 是 → 调 _prepare_dataset(name) 下载并写缓存
   │
   └─ 打开 jsonl，逐行 json.loads → 返回 list[dict]
```

`_prepare_dataset` 内部：

```
_prepare_dataset(name)
   │
   ├─ CACHE_DIR.mkdir(exist_ok=True)
   ├─ out_path = cache/<name>.jsonl
   ├─ tmp_path = cache/<name>.jsonl.<pid>.tmp      ← 临时文件带进程号
   │
   ├─ load_dataset(*load_args, **load_kwargs)       ← 真正联网下载
   │
   ├─ for row in dataset:
   │     turns = format(row)  若 multi_turn 否则 [format(row)]
   │     写一行 {"turns": turns} 到 tmp_path          ← JSONL 每行一个 dict
   │
   ├─ os.replace(tmp_path, out_path)                ← 原子改名
   └─ 统计行数并打印
```

注意所有缓存统一写成 `{"turns": [...]}` 的 JSONL，**抹平了单轮/多轮的差异**：单轮数据集的 `turns` 是长度为 1 的列表，多轮（mt-bench）是长度 >1 的列表。下游（4.4 / u3-l5）只认 `turns` 这个键，不必关心数据源。

#### 4.2.3 源码精读

先看下载器 `_prepare_dataset`：

[_prepare_dataset — dflash/benchmark.py:58-81](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L58-L81)

```python
def _prepare_dataset(name: str) -> Path:
    from datasets import load_dataset

    cfg = DATASETS[name]
    CACHE_DIR.mkdir(exist_ok=True)
    out_path = CACHE_DIR / f"{name}.jsonl"
    tmp_path = out_path.with_name(f"{out_path.name}.{os.getpid()}.tmp")  # 带进程号防并发冲突

    print(f"[download] {name} ...")
    dataset = load_dataset(*cfg["load_args"], **cfg["load_kwargs"])

    with open(tmp_path, "w") as f:
        for row in dataset:
            if cfg.get("multi_turn"):
                turns = cfg["format"](row)
            else:
                turns = [cfg["format"](row)]
            f.write(json.dumps({"turns": turns}) + "\n")
    os.replace(tmp_path, out_path)   # 原子落盘
    ...
```

关键点：

1. **`from datasets import load_dataset` 是函数内导入**（惰性导入）。这样即使没装 `datasets` 库，`import dflash.benchmark` 也不会失败——只有真正下载数据集时才需要它。这与 `__init__.py` 的懒加载哲学一脉相承（u1-l3）。
2. **`tmp_path` 带 `os.getpid()`**：形如 `cache/gsm8k.jsonl.12345.tmp`。如果你开多个进程同时跑评测，它们的临时文件不会互相覆盖；最终 `os.replace` 谁先完成谁就定稿，后到的会整体替换。注意：这只是降低冲突，并不是为并发下载设计的锁。
3. **`cfg.get("multi_turn")`**：`get` 而非 `[]`，因为只有 mt-bench 有这个键，其它四条没有。这也回答了 4.1 的伏笔——`multi_turn` 决定 `format` 返回值被当成列表直接用，还是包成一个单元素列表。
4. **`json.dumps({"turns": turns}) + "\n"`**：每行一个 JSON 对象，行间用换行分隔，这就是 JSONL 格式（JSON Lines）。

再看公开入口 `load_and_process_dataset`：

[load_and_process_dataset — dflash/benchmark.py:84-93](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L84-L93)

```python
def load_and_process_dataset(data_name: str) -> list[dict]:
    if data_name not in DATASETS:
        raise ValueError(f"Unknown dataset '{data_name}'. Available: {list(DATASETS.keys())}")

    path = CACHE_DIR / f"{data_name}.jsonl"
    if not path.exists():
        _prepare_dataset(data_name)

    with open(path) as f:
        return [json.loads(line) for line in f]
```

要点：

1. **白名单校验在前**：传一个不存在的数据集名，立刻报错并列出可用项，而不是等到 `load_dataset` 抛出晦涩的网络错误。
2. **存在性短路**：`if not path.exists()` —— 第二次跑评测时直接跳过下载，读本地缓存。这就是「下载一次、反复使用」的实现。
3. **返回 `list[dict]`**：把整个 jsonl 读进内存。对于这些基准数据集（gsm8k 测试集约 1.3k 条、humaneval 164 条）完全够用。

#### 4.2.4 代码实践

**目标**：亲眼看一次「首次下载 → 生成 `cache/gsm8k.jsonl` → 再次调用直接命中缓存」的过程。

**步骤**：

1. 确保已安装 `datasets`（属于某个后端的可选依赖，或在临时虚拟环境 `pip install datasets`）。
2. 在项目根目录运行：

```python
from dflash.benchmark import load_and_process_dataset

data = load_and_process_dataset("gsm8k")
print("条数:", len(data))
print("第一条:", data[0])
```

3. 检查项目根目录下是否出现 `cache/gsm8k.jsonl`；用文本编辑器或 `head` 看第一行，确认是 `{"turns": ["..."]}` 结构。
4. **再次运行**同一段代码，观察终端是否还出现 `[download] gsm8k ...`（应当不再出现，因为缓存已命中）。
5. 试着在下载过程中途（首次运行时）按 Ctrl-C 打断，再检查 `cache/` 下是否只留下 `*.tmp` 文件而没有残缺的 `gsm8k.jsonl`。

**需要观察的现象**：

- 首次运行打印 `[download] gsm8k ...`，结束后打印 `[cached] <路径> (N samples)`。
- 第二次运行不打印 `[download]`，直接返回数据。
- 打断后 `gsm8k.jsonl` 不会存在（因为还没 `os.replace`），只有临时文件——印证了原子写的安全性。

**预期结果**：`cache/gsm8k.jsonl` 存在，每行形如 `{"turns": ["<问题>\nPlease reason step by step..."]}`。

**待本地验证**：实际样本数与是否需要登录 Hugging Face 取决于你的网络与账号环境；gsm8k 一般可匿名下载。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `_prepare_dataset` 里的 `os.replace(tmp_path, out_path)` 换成「先 `open(out_path, "w")` 再逐行写」，会引入什么风险？

> **答案**：失去原子性。若写到一半进程被杀，`out_path` 会留下残缺文件；下次 `load_and_process_dataset` 看到 `path.exists()` 为真就跳过下载，于是永远用坏缓存，且没有任何报错（静默出错）。`os.replace` 保证了「要么没有、要么完整」。

**练习 2**：为什么 `tmp_path` 要带 `os.getpid()`？

> **答案**：避免多个评测进程同时下载同一数据集时，临时文件名冲突导致互相覆盖/截断。带上进程号后每个进程写自己的临时文件，最后谁先 `os.replace` 谁定稿。

---

### 4.3 CLI 参数解析

#### 4.3.1 概念说明

`benchmark.py` 既能 `import` 当库用（上一节的 `load_and_process_dataset`），也能 `python -m dflash.benchmark ...` 当命令行工具用——这归功于文件末尾的 `if __name__ == "__main__": main()`。`main()` 用标准库 `argparse` 解析命令行，把所有参数塞进一个 `argparse.Namespace` 对象，再交给 4.4 节的分发逻辑。

`argparse` 的核心思想是「**声明式注册**」：你只负责列出「有哪些参数、什么类型、默认值是什么」，解析、类型转换、`--help` 文档、缺失必填项报错都由它自动完成。

#### 4.3.2 核心流程

`main` 的三段结构：

```
main()
  │
  ├─ ① 注册参数（add_argument × N）
  │     ├─ 必填：--backend / --model / --dataset
  │     ├─ 生成控制：--max-new-tokens / --temperature
  │     ├─ 草稿相关：--draft-model / --block-size / --max-samples
  │     └─ 服务/并发：--base-url / --num-prompts / --concurrency / --top-p / --top-k
  │        / --enable-thinking / --timeout-s
  │
  ├─ ② parse_args() → args
  │
  ├─ ③ 一个跨后端的安全断言（--enable-thinking 与某些模型不兼容）
  │
  └─ ④ 按 args.backend 分发（见 4.4）
```

#### 4.3.3 源码精读

参数注册与解析：

[main 的 argparse 参数表 — dflash/benchmark.py:480-500](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L480-L500)

```python
def main() -> None:
    parser = argparse.ArgumentParser(description="DFlash benchmark")
    parser.add_argument("--backend", choices=["transformers", "sglang", "vllm", "mlx"], required=True)
    parser.add_argument("--model", type=str, required=True)
    parser.add_argument("--dataset", type=str, required=True)
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--temperature", type=float, default=0.0)

    parser.add_argument("--draft-model", type=str, default=None)
    parser.add_argument("--block-size", type=int, default=None)
    parser.add_argument("--max-samples", type=int, default=None)

    parser.add_argument("--base-url", type=str, default="http://127.0.0.1:30000")
    parser.add_argument("--num-prompts", type=int, default=1024)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--top-k", type=int, default=1)
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument("--timeout-s", type=int, default=3600)

    args = parser.parse_args()
```

几个值得注意的设计：

1. **`--backend` 用 `choices=`**：限定四个合法值，传错会立即被 argparse 拒绝，省去手写 if 校验。
2. **命令行用 `--max-new-tokens`（连字符），代码里访问 `args.max_new_tokens`（下划线）**：argparse 自动把连字符转成下划线，这是它的约定。
3. **`--draft-model` 默认 `None`**：服务型后端（vLLM/SGLang）不需要 draft model 参数——草稿模型已经在服务启动时的 `--speculative-config` 里配好了（u1-l2），评测端只管发请求。所以这里不设 `required=True`，是否必填交给 4.4 的分发逻辑按后端判断。
4. **`--enable-thinking` 用 `action="store_true"`**：这是一个开关型参数，出现即为 `True`，不出现为 `False`，不需要传值。
5. **`--dataset` 是自由字符串**：它没有用 `choices=` 限定为 `DATASETS.keys()`。真正的白名单校验发生在 `load_and_process_dataset` 里（4.2 节）。这是一个有意的分层：CLI 只做粗解析，数据集名校验放在数据层。

参数解析后有一道跨后端的安全检查：

[--enable-thinking 兼容性断言 — dflash/benchmark.py:502-505](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L502-L505)

```python
assert not (args.enable_thinking and any(x in args.model.lower() for x in ["qwen3-4b", "qwen3-8b"])), (
    "DFlash draft models for Qwen3-4B and Qwen3-8B were not trained with thinking traces. "
    "Using --enable-thinking will lead to suboptimal performance."
)
```

这段断言说明：Qwen3-4B / Qwen3-8B 的 DFlash 草稿模型**训练时没有用思维链（thinking traces）数据**，强行开启 `--enable-thinking` 会让草稿与 target 的分布严重失配，拉低接受长度。这是一个把「领域知识」固化进 CLI 的好例子——在跑长评测之前先 fail-fast，避免浪费时间得到无意义结果。

#### 4.3.4 代码实践

**目标**：不真正跑评测，只观察 `argparse` 的解析行为与报错。

**步骤**：

1. 在项目根目录运行，查看自动生成的帮助：

```bash
python -m dflash.benchmark --help
```

2. 故意不传必填参数，观察报错：

```bash
python -m dflash.benchmark --backend vllm
```

3. 故意传非法 backend：

```bash
python -m dflash.benchmark --backend foo --model X --dataset gsm8k
```

**需要观察的现象**：

- `--help` 列出全部参数、类型与默认值。
- 第 2 条报「`--model` / `--dataset` is required」（或类似），退出码非 0。
- 第 3 条报「invalid choice: 'foo' (choose from transformers, sglang, vllm, mlx)」。

**预期结果**：argparse 自动完成参数校验与友好报错，无需手写。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `--dataset` 不在 `argparse` 里用 `choices=list(DATASETS.keys())` 限定？

> **答案**：分层设计。CLI 层只负责「把字符串收下来」，数据集合法性校验放在数据层 `load_and_process_dataset` 里（用 `ValueError` 报错并列出可用项）。这样 `load_and_process_dataset` 作为库函数被直接 import 时（如 4.2 的实践）也能享受同样的校验，不依赖 CLI。如果校验只放在 argparse，库调用就绕过了校验。

**练习 2**：`--max-new-tokens 2048` 在代码里怎么访问？

> **答案**：`args.max_new_tokens`。argparse 自动把连字符形式的命令行名转成下划线形式的属性名。

---

### 4.4 后端分发逻辑

#### 4.4.1 概念说明

四种后端分成两类：

- **库型**（transformers、mlx）：评测进程直接 `import` 模型、在本进程内跑生成。需要本地加载 target 和 draft 两个模型对象，因此**必须传 `--draft-model`**。
- **服务型**（vllm、sglang）：DFlash 加速已经在另一个服务进程里配好了（见 u1-l2 的 `--speculative-config`），评测进程只是个 HTTP 客户端，**不需要 `--draft-model`**。

`main` 的分发逻辑就是这条「两类三函数」路由：根据 `--backend` 决定调 `_run_transformers`、`_run_mlx` 还是 `_run_server`，并在库型后端上额外检查 `--draft-model` 是否提供。

#### 4.4.2 核心流程

```
backend == "transformers"?
   ├─ --draft-model 缺失？→ parser.error(...)     ← argparse 风格报错并退出
   └─ _run_transformers(args)                       ← 见 u3-l5
backend == "mlx"?
   ├─ --draft-model 缺失？→ parser.error(...)
   └─ _run_mlx(args)                                ← 见 u3-l5
其它（vllm / sglang）?
   └─ _run_server(args)                             ← 不检查 draft-model，见 u3-l5
```

#### 4.4.3 源码精读

[main 的后端分发 — dflash/benchmark.py:507-516](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L507-L516)

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
        _run_server(args)
```

要点：

1. **`parser.error(msg)` 而非 `raise`**：`parser.error` 会以 argparse 统一格式打印错误并 `sys.exit(2)`，比手动抛异常更符合 CLI 工具的惯例（输出更简洁、退出码标准）。
2. **条件必填**：`--draft-model` 在参数注册时是 `default=None`（4.3），是否必填**在这里按后端动态判定**。这比在 `add_argument` 上写死 `required=True` 更灵活——服务型后端若被迫传 draft-model 反而令人困惑。
3. **`else` 兜底到 `_run_server`**：因为 `--backend` 已被 `choices` 限制为四个值，到这里 `else` 实际只可能是 `vllm` 或 `sglang`，两者共用同一个 `_run_server`（内部再按 `args.backend == "vllm"` 细分请求格式，见 u3-l5）。用 `else` 而非 `elif args.backend == "vllm"` 让代码更短，且自动覆盖未来新增的服务型后端。

注意：本讲的边界到此为止。`_run_transformers` 内部的 `torchrun` 分布式、`_run_server` 的 `ThreadPoolExecutor` 并发评测、以及 `_print_decode_summary` 的加速比与接受长度直方图，全部留待 **u3-l5**。

最后，把命令行入口与分发串起来的是文件末尾的两行：

[模块入口 — dflash/benchmark.py:519-520](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L519-L520)

```python
if __name__ == "__main__":
    main()
```

`python -m dflash.benchmark` 会把 `benchmark.py` 当作脚本执行，`__name__` 即为 `"__main__"`，从而触发 `main()`。

#### 4.4.4 代码实践

**目标**：用最小的「假后端」理解分发结构，不依赖任何 GPU/服务。

**步骤**：

1. 在项目根目录起一个 Python 终端，复刻分发骨架（示例代码，非项目原有）：

```python
# 示例代码：模拟 main 的分发结构
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--backend", choices=["transformers", "sglang", "vllm", "mlx"], required=True)
parser.add_argument("--draft-model", default=None)
parser.add_argument("--dataset", required=True)

def fake_run(name):
    print(f"  → 进入 {name}")

def dispatch(args):
    if args.backend == "transformers":
        if args.draft_model is None:
            parser.error("--draft-model is required for transformers backend")
        fake_run("_run_transformers")
    elif args.backend == "mlx":
        if args.draft_model is None:
            parser.error("--draft-model is required for mlx backend")
        fake_run("_run_mlx")
    else:
        fake_run("_run_server")

# 场景 A：服务型后端，不传 draft-model
dispatch(parser.parse_args("--backend vllm --dataset gsm8k".split()))
# 场景 B：库型后端，漏传 draft-model
dispatch(parser.parse_args("--backend transformers --dataset gsm8k".split()))
```

2. 观察场景 A 正常进入 `_run_server`，场景 B 触发 `parser.error` 并以退出码 2 结束。

**需要观察的现象**：

- 场景 A 打印 `→ 进入 _run_server`。
- 场景 B 打印错误信息并 `SystemExit(2)`（在交互式终端表现为抛出 `SystemExit` 异常）。

**预期结果**：库型后端强制 `--draft-model`，服务型后端不强制——与真实 `main` 行为一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么服务型后端（vLLM/SGLang）不需要在评测命令里传 `--draft-model`？

> **答案**：服务型后端的 DFlash 草稿模型在**服务启动时**就通过 `--speculative-config`（vLLM）或 `--speculative-*` 系列 flag（SGLang）配好了（u1-l2）。评测端只是向该服务的 HTTP 接口发 prompt，加速完全在服务端透明完成，客户端无需也无法指定草稿模型。

**练习 2**：如果把 `--backend` 的 `choices` 去掉，4.4 的分发逻辑会有什么隐患？

> **答案**：`else` 分支会把任何非 `transformers`/`mlx` 的值（包括拼写错误如 `vlmm`）都路由进 `_run_server`。`_run_server` 内部用 `is_vllm = args.backend == "vllm"` 判断，那么拼错的 backend 会被当成 SGLang 处理，发出错误格式的请求，产生难以定位的故障。`choices` 在入口处就挡住了非法值。

---

## 5. 综合实践

**任务**：为 benchmark 增加「**boolq**」一个新数据源，并验证它能被 `load_and_process_dataset` 正确处理（不要求真跑生成）。

这个任务把本讲四个模块全部串起来：写配置（4.1）→ 走缓存流程（4.2）→ 用 CLI 传参（4.3）→ 被分发逻辑调用（4.4）。

**步骤**：

1. **理解需求**：boolq 是一个二分类阅读理解数据集，Hugging Face 上为 `google/boolq`，每条数据形如 `{"question": ..., "passage": ..., "answer": ...}`。我们希望 prompt 让模型判断答案并解释。

2. **设计配置**（写在笔记里，**不要在阅读本讲义时改源码**——本任务只做设计与离线验证）。仿照 `DATASETS` 的字段：

```python
# 示例代码：拟新增的配置项（仅用于练习）
"boolq": {
    "load_args": ("google/boolq",),
    "load_kwargs": {"split": "validation"},
    "format": lambda x: "Passage: {passage}\nQuestion: {question}\nAnswer the question with Yes or No, then explain.\nAnswer:".format(**x),
},
```

3. **自检三个字段的一致性**：
   - `load_args` / `load_kwargs` 是否会被 `load_dataset(*load_args, **load_kwargs)` 正确接收？是。
   - `format` 用到的 `passage`、`question` 是否真的是 boolq 行里的键？是（boolq 标准字段）。
   - 没有设 `multi_turn`，所以它会被包成 `[format(row)]` 单轮——符合预期。

4. **离线验证 `format`**（不联网）：

```python
# 示例代码
fmt = lambda x: "Passage: {passage}\nQuestion: {question}\nAnswer the question with Yes or No, then explain.\nAnswer:".format(**x)
print(fmt({"passage": "Cats are mammals.", "question": "Are cats mammals?", "answer": True}))
```

5. **如果你确实想让它生效**：把这条配置追加到 `dflash/benchmark.py` 的 `DATASETS` 字典里（这是允许的二次开发）。然后运行：

```python
from dflash.benchmark import load_and_process_dataset
data = load_and_process_dataset("boolq")   # 首次会下载并写 cache/boolq.jsonl
print(len(data), data[0])
```

并检查 `cache/boolq.jsonl` 是否出现、每行是否是 `{"turns": ["Passage: ..."]}`。

6. **CLI 端验证**：配置生效后，下面这条命令应当能通过参数校验并进入 `_run_server`（即便服务没起，也会在发请求时报连接错误，而不是在校验阶段报「未知数据集」）：

```bash
python -m dflash.benchmark --backend vllm --model Qwen/Qwen3-8B \
    --dataset boolq --base-url http://127.0.0.1:8000
```

**预期结果**：

- 离线 `format` 验证应输出一段含 Passage/Question/Answer 的文本。
- 新增配置后 `load_and_process_dataset("boolq")` 成功返回列表，`cache/boolq.jsonl` 生成。
- CLI 能接受 `--dataset boolq`（说明它通过了 `DATASETS` 白名单校验）。

**待本地验证**：boolq 是否需要 Hugging Face 登录、实际样本数取决于你的网络环境。

---

## 6. 本讲小结

- `dflash/benchmark.py` 是「**库 + CLI**」双形态的评测工具：可 `import` 也可 `python -m dflash.benchmark`。
- **`DATASETS` 是一张声明式配置表**，用 `load_args` / `load_kwargs` / `format`（可选 `multi_turn`）四个字段把「下载哪个仓库、取哪个 split、格式化成什么 prompt」描述清楚；加新数据集只需加一行，不碰控制流。
- 所有缓存统一写成 `{"turns": [...]}` 的 JSONL，**抹平单轮/多轮差异**；下游只认 `turns` 键。
- **下载采用原子写缓存**：先写 `cache/<name>.jsonl.<pid>.tmp`，再 `os.replace` 改名到目标路径，保证「要么没有、要么完整」，杜绝中断后留下残缺缓存。
- `main` 用 `argparse` **声明式注册参数**，自动处理类型转换、`--help`、必填校验；`--backend` 用 `choices` 限定四值，`--dataset` 的白名单校验则下沉到数据层。
- **后端分两类三函数**：库型（transformers/mlx，需 `--draft-model`）与服务型（vllm/sglang，不需 draft-model），由 `main` 末尾的 if/elif/else 路由，库型缺 draft-model 时用 `parser.error` fail-fast。

## 7. 下一步学习建议

本讲只讲到「**数据怎么来、命令怎么解析、任务怎么分发**」，三条运行器的内部还没打开。下一讲 **u3-l5《多后端评测运行器与指标》** 会接着讲：

- `_run_transformers`：如何用 `torchrun` 起多卡分布式评测、`_dist_init` / `_dist_gather` 怎么按 rank 分片与汇总。
- `_run_server`：如何用 `ThreadPoolExecutor` 对 vLLM/SGLang 做并发 HTTP 压测、warmup 机制、吞吐与接受长度统计。
- `_print_decode_summary`：如何同时跑 `block_size=1` 与 `block_size>1`，由两者吞吐比得到加速比，并打印接受长度直方图。

建议你在进入 u3-l5 前，先把本讲的「综合实践」做一遍——亲手加一条 `DATASETS` 配置能让你在阅读运行器代码时，对 `load_and_process_dataset` 返回的数据结构有切身感受。
