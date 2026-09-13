# 快速上手：安装、注册注意力后端并运行示例

## 1. 本讲目标

上一讲（u1-l1）我们建立了 MoBA 的 problem statement：全量注意力随序列长度平方增长的开销，以及「块稀疏 + 无参数 top-k gate」的解法。本讲不动算法细节，解决一个更朴素的问题——**让 MoBA 在你自己的环境里跑起来**。

学完本讲，你应该能够：

1. 用 `pip install .` 安装本项目，并说清楚它依赖哪些库、为什么 `flash-attn` 要精确锁定 `2.6.3` 版本。
2. 解释 `register_moba(MoBAConfig(chunk_size, topk))` 这一行代码如何把两个新注意力后端 `moba` 和 `moba_naive` 注入 transformers，使 `attn_implementation="moba"` 变成合法选项。
3. 独立运行单元测试 `tests/test_moba_attn.py` 中的少量用例，以及用不同 `--attn`、`--moba-chunk-size`、`--moba-topk` 参数组合运行 `examples/llama.py` 完成一次生成，并解释观察到的现象。

## 2. 前置知识

本讲需要以下基础概念，先用一两句话通俗解释：

- **pip 与 pyproject.toml**：`pyproject.toml` 是 Python 项目的打包配置文件。`pip install .` 会读取它，把指定的包安装到当前环境。本项目的配置里，依赖列表不在 `pyproject.toml` 里写死，而是指向一个单独的 `requirements.txt`（后面精读时会看到）。
- **transformers 的注意力分发机制**：Hugging Face transformers 加载模型时接受一个 `attn_implementation` 参数（如 `"eager"`、`"sdpa"`、`"flash_attention_2"`）。较新版本的 transformers 维护了一个全局字典 `ALL_ATTENTION_FUNCTIONS`：字符串键映射到注意力实现函数。每层注意力（如 `LlamaAttention`）的前向计算会拿模型级配置的字符串去这个字典里查函数来调用。**这就是外部代码能"注册"自定义注意力的入口。**
- **`functools.partial`**：Python 标准库工具，把一个函数的若干参数预先"钉死"，返回一个新函数。例如 `partial(f, a)` 得到的函数被调用时等价于 `f(a, ...其余参数)`。
- **dataclass**：Python 的 `@dataclass` 装饰器自动为类生成 `__init__`、`__repr__` 等方法，适合存放纯配置数据。
- **前置概念承接（来自 u1-l1）**：MoBA 把 KV 序列按 `moba_chunk_size` 分块，每个 query 通过无参数 gate（与块内 K 均值的内积）选出 `moba_topk` 个最相关的块参与注意力。长序列 \( n \) 下，注意力的计算量占比约为 \( \frac{\text{topk} \times \text{chunk\_size}}{n} \)。本讲你只需要把这两个数当成"旋钮"来用。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md) | 项目说明 | 环境安装、Quick Start、两种实现定位、单测命令 |
| [pyproject.toml](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/pyproject.toml) | 打包配置 | 包名、Python 版本要求、如何引入依赖 |
| [requirements.txt](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/requirements.txt) | 依赖清单 | 5 个依赖及版本约束 |
| [moba/config.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/config.py) | MoBAConfig 定义 | 两个稀疏度参数 |
| [moba/\_\_init\_\_.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/__init__.py) | 包入口与注册函数 | `register_moba` 的 11 行核心逻辑 |
| [examples/llama.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py) | 端到端示例 | 命令行参数、加载与生成流程 |
| [moba/wrapper.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py) | HF 接口适配层 | 只看 `moba_layer` 的签名与 partial 的对应关系（细节留到 u1-l3 / u4-l1） |
| [tests/test_moba_attn.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py) | 正确性单测 | 实践任务中要运行的用例 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**安装与依赖** → **MoBAConfig** → **register_moba 与 examples/llama.py**。顺序即安装后使用的最短路径。

### 4.1 安装与依赖：pyproject.toml 与 requirements.txt

#### 4.1.1 概念说明

这一模块回答三个问题：

1. `pip install .` 到底装了什么？——只装 `moba` 这一个 Python 包（4 个模块文件），不装示例和测试。
2. 装包时会自动拉取哪些依赖？——由 `requirements.txt` 决定，其中最关键的是精确锁定的 `flash-attn==2.6.3`。
3. 为什么 flash-attn 要锁死版本？——因为 MoBA 的高效实现直接调用了 flash-attn 的**底层内部函数**（如 `_flash_attn_varlen_forward`，u3 单元会精读），这类内部 API 没有跨版本兼容承诺，升级很可能导致签名变化而报错。README 也明确写了这一前提。

#### 4.1.2 核心流程

安装流程伪代码：

```text
pip install .
  ├─ 读 pyproject.toml
  │    ├─ 包名 moba，要求 Python >= 3.10
  │    └─ 依赖是 dynamic 的 → 去读 requirements.txt
  ├─ 按 requirements.txt 安装依赖
  │    flash-attn==2.6.3（精确锁定，需 NVIDIA GPU，编译安装较慢）
  │    transformers>=4.48.3 / accelerate>=1.3.0 / einops>=0.8.1 / torch>=2.1.0
  └─ 发现并安装 moba 包（include = ["moba*"]）
之后才能：from moba import register_moba, MoBAConfig
```

#### 4.1.3 源码精读

先看打包配置：[pyproject.toml:1-14](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/pyproject.toml#L1-L14)

```toml
[project]
name = "moba"
version = "1.0.0"
requires-python = ">=3.10"
dynamic = ["dependencies"]

[tool.setuptools.dynamic]
dependencies = {file = ["requirements.txt"]}

[tool.setuptools.packages.find]
where = ["."]
include = ["moba*"]
```

这段配置声明：包名 `moba`、要求 Python ≥ 3.10；依赖不写在本地而是 `dynamic` 地从 `requirements.txt` 读取；打包时只收集 `moba` 开头的目录——也就是说 `examples/`、`tests/`、`figures/` 都不会被安装，它们只是仓库里的源码资产。

再看依赖清单：[requirements.txt:1-5](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/requirements.txt#L1-L5)

```text
flash-attn==2.6.3
transformers>=4.48.3
accelerate>=1.3.0
einops>=0.8.1
torch>=2.1.0
```

五个依赖各自的用途：

| 依赖 | 用途 | 版本约束的含义 |
| --- | --- | --- |
| `flash-attn==2.6.3` | 提供 varlen 注意力内核，MoBA 高效实现的积木 | 精确锁定：内部 API 依赖 |
| `torch>=2.1.0` | 深度学习框架 | 下限即可，无上限（README 环境说明同样写 `torch >= 2.1.0`） |
| `transformers>=4.48.3` | 提供 `ALL_ATTENTION_FUNCTIONS` 注册机制与 Llama 模型 | 下限与 wrapper.py 适配的注意力接口签名匹配 |
| `accelerate>=1.3.0` | 支持 `device_map="auto"` 的模型分片加载 | 示例脚本需要 |
| `einops>=0.8.1` | 张量重排的简洁表达 | `moba_efficient.py` 内部使用 |

README 中对应的安装说明：[README.md:42-49](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L42-L49) 给出的就是 `conda create -n moba python=3.10` + `pip install .` 两步。

#### 4.1.4 代码实践

1. **实践目标**：确认安装成功，且依赖版本满足约束。
2. **操作步骤**（需要 NVIDIA GPU 环境，flash-attn 仅支持 CUDA）：
   ```bash
   conda create -n moba python=3.10
   conda activate moba
   pip install .
   python -c "from moba import register_moba, MoBAConfig; print('ok')"
   pip show flash-attn transformers torch | grep -E "Name|Version"
   ```
3. **需要观察的现象**：`pip install .` 过程会编译/安装 flash-attn（耗时可能较长）；最后一行命令打印 `ok`；三个库的版本分别满足 `==2.6.3`、`>=4.48.3`、`>=2.1.0`。
4. **预期结果**：`from moba import ...` 成功即说明包结构与依赖全部就绪。若无 GPU 环境，可退化为代码阅读：对照 [requirements.txt](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/requirements.txt) 检查本机 `python -c "import transformers; print(transformers.__version__)"` 是否 ≥ 4.48.3，并在心里回答"缺什么"。（本讲义撰写环境无 GPU，上述运行结果**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：如果同事把 `flash-attn==2.6.3` 改成 `flash-attn>=2.6.3`，可能发生什么？

答案：安装会拉到最新版 flash-attn。由于 MoBA 的高效实现调用的是 flash-attn 的内部函数（如 `_flash_attn_varlen_forward`），新版本若修改了这些非公开 API 的签名或行为，`import` 或运行时会直接报错。这正是精确锁版的原因；README 的环境说明也强调了 `flash-attn==2.6.3` 这一前提。

**练习 2**：`pip install .` 之后，`import moba` 能用，但 `import tests` 呢？

答案：不能（也没必要）。`pyproject.toml` 的 `include = ["moba*"]` 限定只打包 `moba` 目录；`tests/` 与 `examples/` 只是仓库源码，使用时需要先 clone 仓库再从仓库根目录运行。

### 4.2 MoBAConfig：控制稀疏度的两个旋钮

#### 4.2.1 概念说明

`MoBAConfig` 是整个库唯一的配置对象，只有两个整数字段：

- `moba_chunk_size`：KV 分块的大小。块越大，块数越少，每块"代表向量"（K 均值）越粗粒度。
- `moba_topk`：每个 query 最多选择多少个块参与注意力。

这两个数共同决定稀疏度：序列长 \( n \)、块大小 \( c \)、选块数 \( k \) 时，每个 query 实际参与注意力的 KV 数约为 \( k \times c \)，占全量的比例约为 \( \frac{k \times c}{n} \)。承接 u1-l1 的结论：增大 `topk` 或 `chunk_size` 会更接近全量注意力（更准也更慢），减小则更稀疏（更快，但模型需要靠继续训练学会在这种稀疏模式下工作）。

#### 4.2.2 核心流程

```text
用户命令行/脚本
  └─ MoBAConfig(moba_chunk_size=?, moba_topk=?)
       └─ 传给 register_moba(cfg) 预绑定进注意力后端
            └─ 每层注意力前向时读取 cfg.moba_chunk_size / cfg.moba_topk
```

注意它是**进程级全局配置**：一次 `register_moba` 决定了本次运行中所有注意力层使用的参数，而不是每层单独配置。

#### 4.2.3 源码精读

完整源码只有 7 行：[moba/config.py:1-7](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/config.py#L1-L7)

```python
from dataclasses import dataclass


@dataclass
class MoBAConfig:
    moba_chunk_size: int
    moba_topk: int
```

这是一个纯 `@dataclass`，两个字段都**没有默认值**——调用方必须显式给出两个数，避免了"忘了配 topk 而悄悄用了某个默认稀疏度"的隐患。构造方式见示例脚本：[examples/llama.py:21](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L21) 的 `register_moba(MoBAConfig(args.moba_chunk_size, args.moba_topk))`。

作为参照，README 报告的 40 倍加速对应的取值是 `chunk_size=2048, topk=3`（见 [README.md:60-63](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L60-L63)），而 `examples/llama.py` 的默认值是 `4096 / 12`——示例默认更"稠密"，这是正常的工程选择：示例首要目标是能跑通。

#### 4.2.4 代码实践

1. **实践目标**：建立两个参数与稀疏度之间的数量直觉（本实践 CPU 即可，不需要 flash-attn）。
2. **操作步骤**（示例代码，可直接保存为独立脚本运行）：
   ```python
   # 示例代码：只依赖 dataclasses，无需安装 moba
   from dataclasses import dataclass

   @dataclass
   class MoBAConfig:
       moba_chunk_size: int
       moba_topk: int

   n = 32_768  # 假设序列长度 32K，与 README 测试条件同量级
   for c, k in [(2048, 3), (4096, 12), (1024, 2)]:
       cfg = MoBAConfig(moba_chunk_size=c, moba_topk=k)
       attended = cfg.moba_topk * cfg.moba_chunk_size
       print(f"chunk={c}, topk={k}, 每个query参与KV数≈{attended}, "
             f"占比≈{attended / n:.2%}, 块数={n // c}")
   ```
3. **需要观察的现象**：三组配置下"每个 query 参与的 KV 数"与"占全量比例"的变化。
4. **预期结果**：`(2048, 3)` 时每个 query 约参与 6144 个 KV（≈18.75%）；`(4096, 12)` 时为 49152——**超过了 n=32768 本身**，说明这组参数在 32K 序列下已经"稀疏得等于全量"（后续在 u3 会看到代码用 `min` 截断此类情况）；`(1024, 2)` 时仅 2048（≈6.25%），最稀疏也最快。这一步在 CPU 上可复现，但具体打印格式**请本地运行确认**。
5. 顺带验证无默认值的设计：尝试 `MoBAConfig(moba_chunk_size=2048)` 会直接抛 `TypeError`（缺 `moba_topk`），这正是"必须显式配置"的体现。

#### 4.2.5 小练习与答案

**练习 1**：序列长度 8192、`chunk_size=2048`、`topk=3` 时，每个 query 参与的 KV 占比是多少？

答案：块数 = 8192 / 2048 = 4，每个 query 最多选 3 块，参与 KV 数 = 3 × 2048 = 6144，占比 = 6144 / 8192 = 75%。也就是说短序列 + 大块时 MoBA 几乎退化为全量（再叠加因果约束，实际还会更低）。

**练习 2**：为什么 `MoBAConfig` 不给两个字段设置默认值？

答案：这两个参数直接决定模型的计算行为与稀疏模式，不同规模模型/任务的最佳取值差异很大；设置默认值容易让使用者无意识地沿用不适合的配置。强制显式传入是一种"配置即文档"的做法。

**练习 3**：`MoBAConfig(4096, 12)` 和 `MoBAConfig(moba_topk=12, moba_chunk_size=4096)` 等价吗？

答案：等价。前者按位置传参（`chunk_size` 在前），后者按关键字传参。注意位置传参时顺序容易写反，关键字传参更安全。

### 4.3 register_moba：把 MoBA 装进 transformers

#### 4.3.1 概念说明

MoBA 没有自己实现完整的 Llama 模型，而是"寄生"在 transformers 上：transformers 负责模型结构、权重加载、生成循环，MoBA 只替换其中的**注意力计算**。替换的钩子就是 transformers 的全局字典 `ALL_ATTENTION_FUNCTIONS`（从 `transformers.modeling_utils` 导入）。

`register_moba` 做的事情一句话概括：**向这个字典写入两个新键值对**，键是注意力后端名字符串（`"moba"`、`"moba_naive"`），值是用 `partial` 预绑定了具体实现与配置的 `moba_layer`。注册之后，`from_pretrained(..., attn_implementation="moba")` 就能像使用内置后端一样使用 MoBA。

#### 4.3.2 核心流程

```text
register_moba(cfg)
  ├─ ALL_ATTENTION_FUNCTIONS["moba_naive"] = partial(moba_layer, moba_attn_varlen_naive, cfg)
  ├─ ALL_ATTENTION_FUNCTIONS["moba"]       = partial(moba_layer, moba_attn_varlen,     cfg)
  ↓
from_pretrained(..., attn_implementation="moba")
  ├─ transformers 校验 "moba" 是合法注意力实现（已注册 → 通过）
  └─ 每层注意力前向时调用 ALL_ATTENTION_FUNCTIONS["moba"](module, q, k, v, ...)
       └─ 等价于 moba_layer(moba_attn_varlen, cfg, module, q, k, v, ...)
            → 内部转布局、组 varlen 批、调用核心实现（u1-l3 / u3 再展开）
```

关键对应关系（`partial` 的求值）：transformers 调用时传入的 `(module, query, key, value, ...)` 会排到 `partial` 已钉死的两个参数**后面**，正好填进 `moba_layer` 的 `(module, query, key, value, ...)` 形参位。

#### 4.3.3 源码精读

包入口与注册函数全文：[moba/\_\_init\_\_.py:1-11](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/__init__.py#L1-L11)

```python
from functools import partial
from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS
from .wrapper import moba_layer
from .moba_naive import moba_attn_varlen_naive
from .moba_efficient import moba_attn_varlen
from .config import MoBAConfig


def register_moba(cfg: MoBAConfig):
    ALL_ATTENTION_FUNCTIONS["moba_naive"] = partial(moba_layer, moba_attn_varlen_naive, cfg)
    ALL_ATTENTION_FUNCTIONS["moba"] = partial(moba_layer, moba_attn_varlen, cfg)
```

逐行解读：

- 第 1-6 行：导入 `partial`、transformers 的注册表、适配层 `moba_layer`、两个核心实现（naive 与 efficient，即 u1-l1 介绍的两个后端）和配置类。
- 第 10 行：键 `"moba_naive"` 绑定 `moba_attn_varlen_naive`——基于 attention mask 的教学参考实现，便于理解与可视化块选择。
- 第 11 行：键 `"moba"` 绑定 `moba_attn_varlen`——README 所说的生产级高效实现（相对 naive 最高 40 倍）。

`partial` 绑定的目标函数签名见 [moba/wrapper.py:29-40](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L29-L40)：

```python
def moba_layer(
    moba_impl: Callable,
    moba_config: MoBAConfig,
    module: torch.nn.Module,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    *args,
    dropout: float = 0.0,
    scaling: Optional[float] = None,
    **kwargs,
) -> Tuple[torch.Tensor, None]:
```

前两个参数 `moba_impl`、`moba_config` 正是 `partial` 预绑定的位置；`module/query/key/value` 等由 transformers 在每层注意力前向时传入。`moba_layer` 内部如何转布局、如何区分 prefill/decode 属于 u1-l3 和 u4-l1 的内容，本讲只需知道这一层"翻译官"的存在。

一个容易忽略的时序要点：**`register_moba` 必须在 `from_pretrained` 之前调用**。transformers 加载时会校验 `attn_implementation` 字符串是否在已注册的实现中，未注册的字符串无法通过校验。`examples/llama.py` 的代码顺序（第 21 行注册、第 22 行加载）正体现了这一点。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到注册动作对 transformers 全局注册表的影响（CPU 即可，不需要 GPU/flash-attn，只需 `pip install transformers`）。
2. **操作步骤**（示例代码）：
   ```python
   # 示例代码：验证注册机制
   from functools import partial
   from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS

   def fake_layer(impl, cfg, module, q, k, v, **kw):
       return impl, cfg

   before = set(ALL_ATTENTION_FUNCTIONS.keys())
   print("内置后端（部分）:", sorted(before))

   def register_fake(cfg):
       ALL_ATTENTION_FUNCTIONS["moba_fake"] = partial(fake_layer, "fake-impl", cfg)

   register_fake(cfg={"moba_chunk_size": 2048, "moba_topk": 3})
   after = set(ALL_ATTENTION_FUNCTIONS.keys())
   print("新增的键:", after - before)

   fn = ALL_ATTENTION_FUNCTIONS["moba_fake"]
   print("调用结果:", fn(module=None, q=None, k=None, v=None))
   ```
3. **需要观察的现象**：注册前字典里已有的内置键（如 `eager`、`sdpa`、`flash_attention_2`，以本机版本实际输出为准）；`after - before` 输出 `{'moba_fake'}`；最后一次调用返回 `('fake-impl', {...})`——证明调用时 `impl` 与 `cfg` 已被 `partial` 钉死。
4. **预期结果**：与上述一致即可说明 `register_moba` 的机制已理解。（不同 transformers 版本的内置键集合可能略有差异，属正常现象；**待本地验证**。）
5. **无 GPU 替代**：若已在 GPU 环境安装了本项目，可把上面的 `fake_layer` 换成真实的 `from moba import register_moba, MoBAConfig; register_moba(MoBAConfig(2048, 3))`，再打印键集合确认多了 `moba` 与 `moba_naive`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `register_moba` 里两行代码注册的是**同一个** `moba_layer`，却能产生两个行为不同的后端？

答案：差异全部被 `partial` 的第一个预绑定参数 `moba_impl` 捕获：`"moba_naive"` 绑定了 `moba_attn_varlen_naive`（mask 参考实现），`"moba"` 绑定了 `moba_attn_varlen`（flash-attn 高效实现）。`moba_layer` 是共用适配层，按绑定的实现转发。

**练习 2**：如果不调用 `register_moba`，直接 `from_pretrained(..., attn_implementation="moba")`，会发生什么？

答案：transformers 在加载时校验注意力实现字符串，`"moba"` 未注册，无法被识别，加载会报错（具体报错信息随 transformers 版本而异）。所以注册必须发生在加载之前。

**练习 3**：连续调用两次 `register_moba(MoBAConfig(2048, 3))` 和 `register_moba(MoBAConfig(4096, 12))`，最终生效的配置是哪个？

答案：是第二次的 `(4096, 12)`。注册就是往字典里写值，后写覆盖先写，且整个进程内所有层共用最后一次注册的配置。这也提醒我们：`MoBAConfig` 是进程级全局生效的。

### 4.4 examples/llama.py：一次完整的生成

#### 4.4.1 概念说明

`examples/llama.py` 是仓库唯一的端到端示例：加载一个真实的 Llama 模型（默认 `meta-llama/Llama-3.1-8B`），把注意力后端换成 MoBA，对固定提示 `"how are you?"` 做一次贪心生成。它演示了"注册 → 加载 → 生成"的标准三步，也是检验安装是否成功的冒烟测试。

需要预先说明一个**重要预期管理**：u1-l1 讲过 MoBA 需要 continue training 才能发挥稀疏注意力的效果。所以这个示例的目的不是"看 MoBA 生成质量变好"，而是**验证接线正确**——注册生效、布局转换无误、前向反向都能跑。另外（预告 u3-l1 的内容）当序列很短时，高效实现会走一个"无需 MoBA 就退回普通自注意力"的兜底分支，本讲实践里会用到这个事实来解释现象。

#### 4.4.2 核心流程

[examples/llama.py:6-36](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L6-L36) 的执行流程：

```text
解析命令行（--model / --moba-chunk-size / --moba-topk / --attn）
  ↓
register_moba(MoBAConfig(chunk_size, topk))     # ① 注册（必须先于加载）
  ↓
AutoModelForCausalLM.from_pretrained(
    model, device_map="auto", torch_dtype=fp16,
    attn_implementation=args.attn)              # ② 每层注意力改用注册的后端
  ↓
AutoTokenizer 编码 "how are you?"
  ↓
model.generate(max_length=32, do_sample=False)  # ③ 贪心生成 32 token
  ↓
打印 token id 与解码文本
```

#### 4.4.3 源码精读

**第一段：命令行参数定义**，[examples/llama.py:6-19](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L6-L19)

```python
parser.add_argument("--model", type=str, default="meta-llama/Llama-3.1-8B")
parser.add_argument("--moba-chunk-size", type=int, default=4096)
parser.add_argument("--moba-topk", type=int, default=12)
parser.add_argument(
    "--attn",
    default="moba",
    help="choose attention backend",
    choices=["flash_attention_2", "moba", "moba_naive"],
)
```

四个参数就是本讲的"控制面板"：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--model` | `meta-llama/Llama-3.1-8B` | 任意 `AutoModelForCausalLM` 支持的因果 LM（HF hub id 或本地路径） |
| `--moba-chunk-size` | 4096 | 传给 `MoBAConfig` 的块大小 |
| `--moba-topk` | 12 | 传给 `MoBAConfig` 的选块数 |
| `--attn` | `moba` | 注意力后端：`moba`（高效）/ `moba_naive`（参考实现）/ `flash_attention_2`（transformers 内置全量注意力基线） |

注意 `argparse` 会把 `--moba-chunk-size` 转成 `args.moba_chunk_size`（连字符变下划线）。`choices` 里的 `flash_attention_2` 不需要注册——它是 transformers 内置实现，留作全量注意力对照。

**第二段：注册 + 加载**，[examples/llama.py:21-29](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L21-L29)

```python
register_moba(MoBAConfig(args.moba_chunk_size, args.moba_topk))
model = AutoModelForCausalLM.from_pretrained(
    args.model,
    trust_remote_code=True,
    device_map="auto",
    torch_dtype=torch.float16,
    attn_implementation=args.attn,
)
```

第 21 行是全脚本最关键的一行：把两个旋钮打包成 `MoBAConfig` 并完成注册。随后 `from_pretrained` 用 `attn_implementation=args.attn` 让所有注意力层走刚注册的后端；`device_map="auto"`（依赖 accelerate）在多卡/单卡间自动分片；`torch_dtype=torch.float16` 指定半精度加载。

**第三段：生成**，[examples/llama.py:31-36](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L31-L36)

```python
prompt = "how are you?"
input_tokens = tknz.encode(prompt)
input_ids = torch.tensor([input_tokens], device=model.device)
tokens = model.generate(input_ids, max_length=32, do_sample=False)
```

固定提示经 tokenizer 编码为 batch=1 的输入，`max_length=32`（含提示在内最多 32 个 token）、`do_sample=False`（贪心解码，保证同配置下结果可复现）。

**一个理解行为的关键旁证**（预告 u3 单元）：提示只有几个 token，远小于默认 `chunk_size=4096`。此时整个序列只落在"最后一块"里，而高效实现把每个 batch 的最后一块留给普通自注意力支路、选块数按 `moba_topk - 1` 调整——见 [moba/moba_efficient.py:313-321](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L313-L321)：

```python
# we will adjust selective topk to moba_topk - 1, as the last chunk is always chosen
moba_topk = min(moba_topk - 1, num_filtered_chunk)
need_moba_attn = moba_topk > 0

# corner case: if no mobo attn needed, just return self attn
if not need_moba_attn:
    return flash_attn_varlen_func(
        q, k, v, cu_seqlens, cu_seqlens, max_seqlen, max_seqlen, causal=True
    )
```

序列不超过一个块时 `num_filtered_chunk` 为 0，`need_moba_attn` 为假，直接退化为普通因果自注意力。**所以用短提示跑这个示例，三个后端的输出理论上应该一致**——这是个很容易观察到的"现象解释"素材。机制细节在 u3-l1 展开。

#### 4.4.4 代码实践

本讲的主实践（对应任务书）：分别在 GPU 环境跑单测与示例。无 GPU 时完成 B 部分的阅读推演。

**A. GPU 环境**

1. **实践目标**：跑通单测与示例，观察不同后端/参数下输出是否一致，并用源码解释。
2. **操作步骤**：
   ```bash
   # (1) 单测：先看看有哪些用例（全量参数化共 3×4×3×3×3=324 个，别全跑）
   pytest tests/test_moba_attn.py --collect-only -q | head -20
   # 挑 1-2 个用例运行，id 格式以 --collect-only 的输出为准
   pytest "tests/test_moba_attn.py::test_attn_varlen_moba[1-1-512-128-128-2]"   # 待本地验证 id 格式

   # (2) 示例：三个后端各跑一次（默认模型为 gated 模型，需先在 HF 申请并登录；
   #     或换成本地可用的任意 causal LM 路径）
   python3 examples/llama.py --attn moba          > out_moba.txt
   python3 examples/llama.py --attn moba_naive    > out_naive.txt
   python3 examples/llama.py --attn flash_attention_2 > out_fa2.txt

   # (3) 改参数再跑（体会两个旋钮）
   python3 examples/llama.py --attn moba --moba-chunk-size 2048 --moba-topk 3
   ```
3. **需要观察的现象**：
   - 单测用例通过，终端打印 `output diff:` 与 `grad diff:` 的 max/mean 数值（来自 [tests/test_moba_attn.py:84](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L84) 的 print，数值在 bf16 容忍度内）。
   - 三个后端、不同 chunk/topk 下，`out_moba.txt`、`out_naive.txt`、`out_fa2.txt` 的解码文本**预期相同**。
4. **预期结果解释**：提示仅约 4 个 token，短于任何 chunk_size，走 [moba_efficient.py:318-321](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L318-L321) 的 self-attn 兜底分支（naive 实现同理：所有 token 都在同一块内，掩码不裁剪任何位置），所以三种配置实际执行的都近似普通因果注意力，输出一致。真正区分后端要靠长序列（单测里 seqlen 512-2048、chunk 128 的组合才能真正触发选块）。（运行结果**待本地验证**——本环境无 GPU，未实际执行。）

**B. 无 GPU 环境的替代实践（源码阅读型）**

1. **实践目标**：不运行代码也能完整推演示例的每一步。
2. **操作步骤**：对照 [examples/llama.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py) 全文 36 行，在纸上回答五个问题：
   1. `--attn moba_naive --moba-topk 5` 时，第 21 行注册的 `MoBAConfig` 内容是什么？绑定的 `moba_impl` 是哪个函数？
   2. 把 `register_moba(...)` 移到 `from_pretrained` 之后会发生什么？
   3. `attn_implementation="flash_attention_2"` 时，第 21 行的注册还有意义吗？（答：无影响，加载走内置实现，注册的键只是闲置。）
   4. `do_sample=False` 与 `max_length=32` 分别控制什么？为什么对比实验要固定它们？
   5. 为什么提示 `"how are you?"` 触发不了块选择？（用 4.4.3 最后一段的兜底分支解释。）
3. **预期结果**：五问全部能不查资料作答，即达到本讲要求。

#### 4.4.5 小练习与答案

**练习 1**：想让示例真正跑进 MoBA 的稀疏选块路径，最直接的办法是什么？

答案：让序列长度显著大于 chunk_size。例如 `--moba-chunk-size 128` 时，任何几十 token 以上的提示就会切出多个块，`num_filtered_chunk > 0`，进入选块逻辑。更系统的验证方式是跑单测中 seqlen=2048、chunk=128 这类参数组合。

**练习 2**：`--attn` 的三个选项中，哪个不依赖 `register_moba`？为什么它也被列在 choices 里？

答案：`flash_attention_2`，它是 transformers 内置的全量注意力后端，与本项目注册无关。列出它是为了提供一个未做任何稀疏化的基线，方便对比 MoBA 后端的行为。

**练习 3**：示例用 `torch_dtype=torch.float16` 加载，而单测 [tests/test_moba_attn.py:44](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L44) 用的是 `torch.bfloat16`。这两处分别说明什么？

答案：说明两个后端对半精度类型都可用（flash-attn 内核支持 fp16/bf16）。示例选 fp16 是常规推理精度；测试选 bf16 并配 `eps=2e-2` 的容忍度（[tests/test_moba_attn.py:45](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L45)），因为测试比较的是两种实现在低精度下的数值一致性，需要容忍舍入误差。

## 5. 综合实践

**任务：给"三种后端 × 两种稀疏度"做一张接线验证表。**

有 GPU 的读者，执行以下步骤并填表；无 GPU 的读者完成同表的"预期"列并写明推断依据（引用源码行号）：

| 后端 | chunk_size | topk | 生成文本是否一致（预期/实测） | 一致/不一致的源码依据 |
| --- | --- | --- | --- | --- |
| moba vs flash_attention_2 | 4096 | 12 | ？ | 提示远短于块 → 兜底分支（moba_efficient.py:318-321） |
| moba vs moba_naive | 4096 | 12 | ？ | 同上；且 naive 对单块序列掩码不裁剪 |
| moba vs flash_attention_2 | 128 | 2 | ？ | 提示若 >128 token 则真触发热路径 → 预期**可能不一致**（模型未经 continue training，见 README Note） |
| moba | 2048 | 3 vs 12 | ？ | 短提示下两者都走兜底 → 预期一致 |

步骤：

1. 按第 4.1 节实践完成安装；按 4.4.4 跑通单测的 1 个用例确认环境可用。
2. 依次运行上表配置的 `python3 examples/llama.py ...`，保存输出。
3. 填"实测"列，与"预期"列对比；不一致时回到源码找依据（本讲覆盖范围内的依据：注册机制、config、兜底分支；超出的记为问题，带到 u2/u3）。
4. 附加题：把提示改长（例如粘贴一段超过 128 token 的文本到 `prompt` 变量的副本脚本中，标注为示例代码），重跑第 3 行配置，观察是否出现差异。

（本环境无 GPU，表中"实测"列**待本地验证**。）

## 6. 本讲小结

- 安装即 `conda` + `pip install .`：`pyproject.toml` 把依赖转交给 `requirements.txt`，其中 `flash-attn==2.6.3` 精确锁定，因为高效实现依赖其内部 API。
- `MoBAConfig(moba_chunk_size, moba_topk)` 是仅有的两个稀疏度旋钮，无默认值、进程级全局生效；每个 query 参与的 KV 数约为 `topk × chunk_size`。
- `register_moba` 用 `partial(moba_layer, 实现, cfg)` 向 transformers 的 `ALL_ATTENTION_FUNCTIONS` 写入 `"moba"` 与 `"moba_naive"` 两个键，且**必须先于** `from_pretrained(attn_implementation=...)` 调用。
- `examples/llama.py` 三步走：注册 → `attn_implementation` 加载 → 贪心生成；`--attn` 还支持内置 `flash_attention_2` 作为全量基线。
- 短提示（序列短于一个块）时高效实现走 `need_moba_attn=False` 的 self-attn 兜底分支，因此示例中三种后端输出预期一致——示例验证的是接线，不是稀疏效果。
- 单测 `pytest tests/test_moba_attn.py` 以 naive 实现为黄金参考、参数化共 324 个用例，跑少量用例即可完成安装冒烟。

## 7. 下一步学习建议

到这里，MoBA 对你来说已经"能装、能注册、能跑"。但我们在 4.3 里刻意绕过了 `moba_layer` 内部的事情：`hf_to_fa` 为什么要把 `[batch, heads, seqlen, dim]` 换成 `[batch*seqlen, heads, dim]`？`cu_seqlens` 这串累加长度到底怎么表达"变长批"？GQA 的 q_heads 和 kv_heads 是什么关系？——这些是读懂核心实现的字母表。下一讲 **u1-l3《读懂源码前的必备知识：张量布局、varlen 与 flash-attn》** 将用 `moba/wrapper.py` 和 `tests/test_moba_attn.py` 的测试数据构造把这三件事一次讲清，之后你就可以进入 u2 单元逐行精读 `moba_naive.py` 了。
