# CLI 入口 tilert.generate 与生成流程

## 1. 本讲目标

本讲是入门层的第四篇。上一篇（u1-l3）已经讲清了「`import tilert` 不会加载后端、`load_backend(model_type)` 才显式把某个 `.so` 拉进进程、且单进程只能装一个」这一加载机制。本讲顺着这条线索往上走一层，回答一个新的问题：

**用户在命令行敲下的 `python -m tilert.generate ...` 到底是怎么一步步变成一次模型生成的？**

学完本讲你应该能够：

1. 掌握 `python -m tilert.generate` 的全部命令行参数及其默认值，能看懂 `--help` 输出背后的 argparse 定义。
2. 理解权重目录的两种来源——命令行 `--model-weights-dir` 与 `~/.tilert/config.toml`——以及它们的优先级和报错行为。
3. 看懂 `get_generator` 如何按 `model_type`（`deepseek_v3_2` / `glm5`）选择对应的生成器类与采样参数，完成「加载后端 → 构造生成器」的分发。
4. 区分 CLI 的两种运行分支：交互模式（`--interactive`）与默认的基准测试（benchmark）模式，并理解一个反直觉的细节——基准模式下 `--with-mtp` 会被忽略。

本讲只覆盖 CLI 这一层。生成器内部的 `from_pretrained / generate / cleanup` 生命周期会在 u1-l5 详讲，解码主循环留到 u3-l2，这里只把它们当作「被 CLI 调用的黑盒」。

## 2. 前置知识

- **CLI 与 argparse**：Python 标准库 `argparse` 用来解析命令行参数。每个 `--xxx` 都对应一次 `parser.add_argument(...)` 调用，定义了参数的类型、默认值和帮助文字。`python -m tilert.generate` 表示「把 `tilert/generate.py` 当作模块入口运行」，触发其 `if __name__ == "__main__":` 块。
- **TOML 配置文件**：一种键值对配置格式（`[section]` 下写 `key = "value"`）。Python 3.11+ 内置 `tomllib` 可以解析它。TileRT 用 `~/.tilert/config.toml` 存放权重路径，避免每次运行都写一长串路径。
- **承自 u1-l3 的关键认知**：两个模型族（DeepSeek-V3.2 与 GLM-5）各自编译成独立后端 `.so`，`tilert.load_backend(model_type)` 负责按需懒加载其中一个，且单进程互斥。本讲的 `get_generator` 正是 `load_backend` 的第一个真实调用方。
- **模型类型（model_type）**：字符串 `"deepseek_v3_2"` 或 `"glm5"`，是整个 CLI 分发的核心 key——它同时决定加载哪个后端、构造哪个生成器类、从配置文件哪个键读权重路径。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [tilert/generate.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py) | CLI 入口。包含 `parse_args()`（参数解析）、`get_generator()`（模型分发）和 `__main__` 块（编排加载→生成→清理的完整流程）。 |
| [tilert/benchmark/config.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/config.py) | 配置加载。`get_weights_dir()` 负责按优先级解析权重目录（CLI 覆盖 > config.toml）。 |
| [tilert/benchmark/__init__.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py) | 基准测试核心。定义 `BenchMode`（采样模式）与 `apply_mode`（运行时切换采样参数），被 CLI 的默认分支调用。 |

> 提示：`generate.py` 顶部 `import tilert` 之后才能调用 `tilert.load_backend`。该函数的内部机制（ctypes + torch.ops.load_library）已在 u1-l3 讲透，本讲不再重复，只关注「谁在什么时机调用它」。

## 4. 核心概念与源码讲解

### 4.1 CLI 参数解析

#### 4.1.1 概念说明

`tilert/generate.py` 既是模块也是脚本。当你执行 `python -m tilert.generate ...` 时，Python 会运行文件底部的 `if __name__ == "__main__":` 块，而该块的第一件事就是调用 `parse_args()`。

`parse_args()` 用标准库 `argparse` 把命令行字符串转换成一个带属性的 `Namespace` 对象。理解 CLI 的关键不是去背参数，而是建立「**每个 `--flag` 都对应源码里一行 `add_argument`**」的对照能力——这样看到 `--help` 输出就能立刻定位到源码定义。

#### 4.1.2 核心流程

```text
python -m tilert.generate --model glm5 --max-new-tokens 100
        │
        ▼
parse_args()                            # argparse 解析全部 --flag
  ├── ArgumentParser(description=...)
  ├── 多次 parser.add_argument(...)      # 每个 flag 一行定义
  └── return parser.parse_args()         # 得到 args 命名空间
        │
        ▼
args.model / args.max_new_tokens / ...   # 后续代码按属性名读取
```

`argparse` 的命名规则很重要：`--max-new-tokens`（短横线）在解析后会变成 `args.max_new_tokens`（下划线）。后续所有代码都按下划线属性名访问参数。

#### 4.1.3 源码精读

`parse_args` 的全部定义在 [tilert/generate.py:75-150](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L75-L150)，这段代码定义了 CLI 的全部参数。

其中最关键的几个参数：

`--model` 用 `choices` 限制了只能选两个模型族，默认是 DeepSeek-V3.2，见 [tilert/generate.py:83-89](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L83-L89)——这行决定了后续 `get_generator` 和 `get_weights_dir` 都拿 `deepseek_v3_2` 或 `glm5` 作为分发 key：

```python
parser.add_argument(
    "--model",
    type=str,
    default="deepseek_v3_2",
    choices=["deepseek_v3_2", "glm5"],
    help="Model type to use (default: deepseek_v3_2).",
)
```

`--max-new-tokens` 默认 4000（注意 README 示例里常写 1000，那是示例传参，不是默认值），见 [tilert/generate.py:90](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L90)。`--top-p` 默认 1.0，且语义是「**小于 1.0 才启用** top-p 采样」，见 [tilert/generate.py:92-97](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L92-L97)。

`--modes` 与 `--workloads` 是基准测试模式下的两个过滤器，都是「逗号分隔字符串」，默认 `None` 表示「全部」，见 [tilert/generate.py:133-144](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L133-L144)：

```python
parser.add_argument(
    "--modes",
    type=str,
    default=None,
    help="Comma-separated mode filters: top-k1,top-p0.95 (default: all)",
)
```

为方便对照，下面把 `parse_args` 定义的全部参数整理成表：

| 参数（flag） | 属性名 | 类型 | 默认值 | 作用 |
|---|---|---|---|---|
| `--model-weights-dir` | `model_weights_dir` | str | `None` | 权重目录；省略则从 config.toml 解析 |
| `--model` | `model` | str | `deepseek_v3_2` | 模型族，二选一 |
| `--max-new-tokens` | `max_new_tokens` | int | `4000` | 最大生成 token 数 |
| `--temperature` | `temperature` | float | `1.0` | 采样温度 |
| `--top-p` | `top_p` | float | `1.0` | top-p 阈值，`<1.0` 启用 |
| `--top-k` | `top_k` | int | `256` | top-k 阈值 |
| `--interactive` | `interactive` | flag | `False` | 进入交互模式 |
| `--with-mtp` | `with_mtp` | flag | `False` | 启用 MTP（仅在交互模式生效，见 4.1.4） |
| `--use-random-weights` | `use_random_weights` | flag | `False` | 用随机权重（测试用） |
| `--enable-thinking` | `enable_thinking` | flag | `False` | chat template 思考模式 |
| `--sampling-seed` | `sampling_seed` | int | `42` | 请求级采样种子 |
| `--model-name` | `model_name` | str | `None` | 覆盖基准表显示名 |
| `--tag` | `tag` | str | `None` | 回归绘图目录标签 |
| `--modes` | `modes` | str | `None` | 基准模式过滤（逗号分隔） |
| `--workloads` | `workloads` | str | `None` | workload 过滤（逗号分隔） |
| `--enable-logprobs` | `enable_logprobs` | flag | `False` | 导出 top-256 logprobs |

#### 4.1.4 代码实践

**实践目标**：建立「`--help` 输出 ↔ 源码 `add_argument`」的精确对照能力。

**操作步骤**：

1. 在已按 u1-l3 搭好环境的容器内执行：

   ```bash
   python -m tilert.generate --help
   ```

2. 对照上方的参数表，逐行确认每个 flag 的 `help` 文字、`default` 是否与 [tilert/generate.py:75-150](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L75-L150) 中的定义一致。

**需要观察的现象**：

- `--help` 不需要 GPU、也不加载后端，因为它只走 argparse，不会触发 `__main__` 里后续的 `load_backend` / `from_pretrained`。所以即使没有权重，这条命令也能正常打印帮助。
- 注意 `--with-mtp` 的 help 写的是「Enable MTP」，但默认值是 `False`。

**预期结果**：你能在帮助输出里找到上表全部 16 个参数；任何多写或少写都说明你看的 HEAD 与本讲不一致。

**待本地验证**：`--help` 在无 GPU 环境下是否能正常退出（按本讲对源码的阅读应当可以，因为它只是 argparse）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--max-new-tokens` 在代码里写作 `max_new_tokens`（下划线），而命令行里写 `--max-new-tokens`（短横线）？

**参考答案**：这是 argparse 的约定——命令行的短横线形式在解析后会被自动转换成 Python 合法的属性名（下划线），所以代码里用 `args.max_new_tokens` 访问。

**练习 2**：如果用户传了一个 `--model` 不在 `choices` 里的值（比如 `--model llama`），会发生什么？是在哪一行被拦下的？

**参考答案**：argparse 会直接报错退出（`error: argument --model: invalid choice`），在 [tilert/generate.py:83-89](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L83-L89) 的 `choices=["deepseek_v3_2", "glm5"]` 处被拦下，根本不会进入后续流程。

### 4.2 权重目录解析

#### 4.2.1 概念说明

TileRT 的权重是「**预先转换好的 per-device 分片布局**」（u1-l6 会详讲转换过程），体积巨大且路径因机器而异。每次运行都把几长的路径写在命令行里很痛苦，于是 TileRT 提供了两种指定权重目录的方式：

1. **命令行直接指定**：`--model-weights-dir /path/to/weights`。
2. **配置文件登记一次**：把路径写进 `~/.tilert/config.toml`，之后 CLI 自动读取。

`get_weights_dir()` 就是把这两种来源按优先级合并起来的解析器，位于 [tilert/benchmark/config.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/config.py)。

#### 4.2.2 核心流程

解析遵循「**命令行优先，配置兜底**」的两级优先级：

```text
get_weights_dir(model, cli_override)
  │
  ├── 1. cli_override 不为 None？  ──是──▶ 直接返回 cli_override（最高优先级）
  │
  └── 2. 否则读 ~/.tilert/config.toml
            ├── 配置文件不存在 ──▶ FileNotFoundError（提示如何创建）
            ├── TOML 语法错误   ──▶ ValueError
            └── [weights] 下没有该 model 键 ──▶ KeyError（列出可用键）
            │
            └── 返回 config["weights"][model]
```

注意 `model` 参数就是 `--model` 的值（`deepseek_v3_2` 或 `glm5`），它既是后端加载的 key，也是配置文件里查权重路径的 key——一物两用。

#### 4.2.3 源码精读

函数签名与优先级注释清清楚楚，见 [tilert/benchmark/config.py:25-34](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/config.py#L25-L34)：

```python
def get_weights_dir(model: str, cli_override: str | None = None) -> str:
    """Resolve the weights directory for *model*.

    Resolution order (highest priority first):
      1. *cli_override* (from ``--model-weights-dir`` CLI flag)
      2. ``~/.tilert/config.toml`` → ``[weights].<model>``
    """
```

第一优先级：只要命令行给了 `--model-weights-dir`，立即返回，连配置文件都不读，见 [tilert/benchmark/config.py:35-36](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/config.py#L35-L36)：

```python
if cli_override is not None:
    return cli_override
```

第二优先级的失败路径设计得非常友好——配置文件不存在时，报错信息里直接给出了创建命令，见 [tilert/benchmark/config.py:39-48](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/config.py#L39-L48)。这一段会打印出应写入的 `[weights]` 模板，初学者照着复制即可。

配置文件解析用 Python 3.11+ 内置的 `tomllib`，见 [tilert/benchmark/config.py:50-56](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/config.py#L50-L56)：

```python
try:
    with open(config_path, "rb") as f:
        config = tomllib.load(f)
except tomllib.TOMLDecodeError as e:
    raise ValueError(...) from e
```

注意它以**二进制模式**（`"rb"`）打开——这是 `tomllib.load` 的硬性要求（`tomllib` 只接受二进制文件对象）。

最后，如果 `[weights]` 段下找不到对应 model 键，抛 `KeyError` 并列出当前可用的所有键，见 [tilert/benchmark/config.py:58-67](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/config.py#L58-L67)。成功命中则返回路径字符串，见 [tilert/benchmark/config.py:69](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/config.py#L69)。

调用方在 `__main__` 块里，`config_key` 直接取自 `args.model`，见 [tilert/generate.py:186-190](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L186-L190)：

```python
config_key = args.model
...
model_weights_dir = get_weights_dir(config_key, cli_override=args.model_weights_dir)
```

#### 4.2.4 代码实践

**实践目标**：亲手触发 `get_weights_dir` 的三条路径（CLI 覆盖、配置命中、配置缺失），观察不同报错。

**操作步骤**：

1. **CLI 覆盖路径**：写一个最小脚本（示例代码，不是项目原有文件）：

   ```python
   # 示例代码：verify_weights_dir.py
   from tilert.benchmark.config import get_weights_dir
   print(get_weights_dir("deepseek_v3_2", cli_override="/tmp/fake-weights"))
   ```
   预期直接打印 `/tmp/fake-weights`，不触碰任何配置文件。

2. **配置缺失路径**：确保 `~/.tilert/config.toml` 不存在，然后运行：

   ```python
   from tilert.benchmark.config import get_weights_dir
   get_weights_dir("deepseek_v3_2")   # 不传 cli_override
   ```
   预期抛 `FileNotFoundError`，且报错信息里带有 `mkdir -p ~/.tilert` 的创建提示。

3. **配置命中路径**：按报错提示创建 `~/.tilert/config.toml`，写入：

   ```toml
   [weights]
   deepseek_v3_2 = "/tmp/fake-weights"
   ```
   再次调用 `get_weights_dir("deepseek_v3_2")`，预期返回 `/tmp/fake-weights`。

**需要观察的现象**：第 1 步与第 3 步返回值相同，但走的代码路径完全不同（一个在第 36 行 return，一个在第 69 行 return）。

**预期结果**：三条路径的行为与 4.2.2 的流程图完全吻合。

#### 4.2.5 小练习与答案

**练习 1**：如果 `--model-weights-dir` 给了一个**不存在**的路径，`get_weights_dir` 会报错吗？

**参考答案**：不会。`get_weights_dir` 只负责「解析出路径字符串」，不做路径存在性校验。错误会推迟到后续 `from_pretrained()` 真正读权重文件时才暴露。这是职责分离的设计——解析与加载分开。

**练习 2**：为什么 `open(config_path, "rb")` 要用二进制模式 `"rb"`？

**参考答案**：因为 `tomllib.load()` 要求传入二进制文件对象（它内部按字节解析 UTF-8）。如果用文本模式 `"r"` 会在 `tomllib.load` 处直接抛 `TypeError`。

### 4.3 get_generator 模型分发

#### 4.3.1 概念说明

解析完参数、拿到权重路径后，CLI 要构造一个「生成器」对象。问题在于：TileRT 有两个模型族，它们的后端 `.so`、生成器类、超参数类都不一样。`get_generator()` 就是这个分叉点——它根据 `model_type` 字符串：

1. 调用 `tilert.load_backend(model_type)` 加载对应后端（u1-l3 讲过的懒加载入口）；
2. **延迟导入**对应的生成器类与超参数类；
3. 用统一的参数列表构造生成器并返回。

这里有一个承自 u1-l3 的关键约束：因为单进程只能装一个后端，所以 `get_generator` 一旦为某个模型族加载了后端，本进程就锁死在这个模型族上了。

#### 4.3.2 核心流程

```text
get_generator(model_type, ...)
  │
  ├── tilert.load_backend(model_type)     # ① 先加载后端 .so（互斥，见 u1-l3）
  │
  ├── if model_type == "deepseek_v3_2":
  │       延迟 import DSAv32Generator, DSAv32ModelArgs
  │       return DSAv32Generator(model_args=DSAv32ModelArgs(), ...)   # ②
  │
  ├── if model_type == "glm5":
  │       延迟 import GLM5Generator, ModelArgsGLM5
  │       return GLM5Generator(model_args=ModelArgsGLM5(), ...)       # ③
  │
  └── raise ValueError(...)               # 理论上不可达（argparse choices 已拦）
```

注意第 ① 步与第 ②/③ 步的**顺序**：必须先 `load_backend`，再 import 生成器类。因为生成器类内部会引用 `torch.ops.tilert.*` 算子，而这些算子只有在那次 `load_library` 之后才被注册到命名空间里。这也是为什么两个生成器类用「**函数内延迟导入**」而不是写在文件顶部——避免在还没加载任何后端时就触发对算子的引用。

#### 4.3.3 源码精读

`get_generator` 的签名与 docstring 明确点出「两个生成器是分开的库、单进程只装一个、生成器延迟导入」三点，见 [tilert/generate.py:20-36](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L20-L36)：

```python
def get_generator(...) -> "DSAv32Generator | GLM5Generator":
    """Load the matching backend .so and build the generator for ``model_type``.

    DeepSeek-V3.2 and GLM-5 ship as separate libraries; only one backend loads
    per process. Generators are imported lazily after the backend is loaded.
    """
    tilert.load_backend(model_type)
```

注意返回类型注解 `"DSAv32Generator | GLM5Generator"` 是字符串形式——因为这两个类在文件顶部只是 `TYPE_CHECKING` 下的占位（见 [tilert/generate.py:9-11](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L9-L11)），运行时并未真正导入，只在类型检查时生效。这正是「延迟导入」的体现。

DeepSeek-V3.2 分支：在函数体内 import，构造 `DSAv32Generator`，见 [tilert/generate.py:38-53](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L38-L53)。其中一个值得注意的细节是 `use_topp=top_p < 1.0`——是否启用 top-p 采样不是单独的开关，而是**由 `top_p` 是否小于 1.0 推导**出来的：

```python
return DSAv32Generator(
    model_args=DSAv32ModelArgs(),
    ...
    top_p=top_p,
    top_k=top_k,
    use_topp=top_p < 1.0,      # 由 top_p 隐式推导
    ...
)
```

GLM-5 分支结构与 DeepSeek 完全对称，只是换成了 `GLM5Generator` 与 `ModelArgsGLM5`，见 [tilert/generate.py:55-70](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L55-L70)。两个模型族都接收同一套参数名（`max_new_tokens`、`temperature`、`with_mtp`、`top_p`、`top_k` 等），这正是「统一参数、分发构造」的设计。

兜底的 `raise ValueError` 在 [tilert/generate.py:72](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L72)。由于 argparse 的 `choices` 已经把非法 `--model` 拦在前面，这行在 CLI 路径下理论不可达；它保护的是「别人直接调用 `get_generator` 函数」的编程式用法。

#### 4.3.4 代码实践

**实践目标**：验证「先 load_backend、后 import 生成器」这一顺序的必要性，并理解 `use_topp` 的隐式推导。

**操作步骤**（源码阅读型实践，无需真实权重）：

1. 在 [tilert/generate.py:36-39](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L36-L39) 处确认调用顺序：`load_backend` 在前，`from tilert.models.deepseek_v3_2.generator import DSAv32Generator` 在后。思考：如果把这两行对调，在尚未加载后端时就 import 生成器，会发生什么？

2. 追踪 `use_topp` 的取值：CLI 默认 `--top-p 1.0`（[generate.py:92-97](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L92-L97)），传给 `get_generator` 后 `use_topp = 1.0 < 1.0 = False`。也就是说**默认走的是 top-k（贪心-ish）采样，不是 top-p**。

3. **预测验证**：若用户传 `--top-p 0.9`，则 `use_topp = 0.9 < 1.0 = True`，启用 top-p 采样。

**需要观察的现象**：`use_topp` 没有独立的 CLI flag，完全由 `--top-p` 的值是否严格小于 1.0 决定。

**预期结果**：能向自己解释「为什么不需要单独的 `--use-topp` 开关」——因为 `top_p < 1.0` 本身就是一个无歧义的信号。

**待本地验证**：在真实环境中分别用默认参数与 `--top-p 0.9` 各跑一次，观察基准表的「top-p0.95 w/ MTP」一行采样行为是否不同（这需要 GPU 与权重，属于进阶验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DSAv32Generator` 和 `GLM5Generator` 的 import 写在 `get_generator` 函数体内部，而不是文件顶部？

**参考答案**：两个原因。其一，必须先 `load_backend` 注册 `torch.ops.tilert.*` 算子，生成器类内部才能引用这些算子；其二，延迟导入让两个模型族的模块互不耦合——单进程只会 import 实际使用的那个，避免在顶部就把两个后端的 Python 胶水代码都加载进来。

**练习 2**：`get_generator` 末尾的 `raise ValueError(f"unsupported model_type: {model_type!r}")` 在 CLI 路径下会被触发吗？为什么还要写？

**参考答案**：CLI 路径下不会触发，因为 argparse 的 `choices=["deepseek_v3_2", "glm5"]` 已在解析阶段拦截了非法值。但这行保护的是 `get_generator` 作为**编程式 API** 被其他代码直接调用时的健壮性——别人可能传入任意字符串，这行能给出清晰报错而不是静默返回 `None`。

---

### 4.4 把三块拼起来：`__main__` 的完整编排

上面三个模块是零件。`__main__` 块把它们串成一条完整的执行链，理解这条链才算真正看懂了 CLI。完整代码在 [tilert/generate.py:153-299](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L153-L299)，核心顺序如下：

```text
args = parse_args()                                    # ① 解析参数
model_weights_dir = get_weights_dir(args.model, ...)   # ② 解析权重目录
with_mtp = args.with_mtp if args.interactive else True # ③ ⚠️ 反直觉点
generator = get_generator(args.model, ...)             # ④ 加载后端 + 构造生成器
generator.from_pretrained()  /  init_random_weights()  # ⑤ 加载权重
if args.interactive:                                   # ⑥ 分支
    while ...: generator.generate(prompt)              #    交互模式
else:
    对每个 BenchMode × 每个 workload 跑基准             #    基准模式
    print_summary_table(...)                           #    打印汇总表
generator.cleanup()                                    # ⑦ 收尾
```

这里有一个**最容易被坑的反直觉细节**，必须单独点出。第 ③ 步在 [tilert/generate.py:192-195](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L192-L195)：

```python
if args.interactive:
    with_mtp = args.with_mtp
else:
    with_mtp = True
```

也就是说——**在默认的基准模式下，`--with-mtp` 这个 flag 会被完全忽略，生成器永远以 `with_mtp=True` 加载 MTP 权重**。`--with-mtp` 只在 `--interactive` 交互模式下才生效。

为什么？因为基准测试套件本身就要同时跑「w/o MTP」和「w/ MTP」两种模式（见 [generate.py:237-248](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L237-L248) 构造的三个 `BenchMode`）。要支持「w/ MTP」模式，生成器必须事先加载好 MTP 权重（`self.with_mtp=True`），否则 `generate()` 会抛 `ValueError("Cannot use MTP mode: MTP weights were not loaded")`。所以基准模式干脆强制加载 MTP 权重，再由每个 `BenchMode.with_mtp` 字段控制单次运行是否实际启用。

这是入门阶段最容易误解的地方：初学者看到 `--with-mtp` 默认 `False`，会以为基准模式默认不开 MTP；实际上基准模式根本不看这个 flag。生成器内部的这个保护性校验在 [tilert/models/deepseek_v3_2/generator.py:176-178](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py) 对应的 generator 文件中。

> 关于 `BenchMode` 与 `apply_mode` 如何在运行时切换采样参数、汇总表三行（tok/s、it/s、acc）的统计来源，会在 u3-l5 专讲；本讲只需知道基准分支会循环调用 `runner(generator, modes)` 即可。

## 5. 综合实践

**任务**：以「最小改动预测行为」的方式，验证你对 CLI 参数解析、权重目录解析、`with_mtp` 反直觉点三处的理解。

**操作步骤**：

1. **参数对照**：运行 `python -m tilert.generate --help`，把输出与 4.1.3 的参数表逐项比对。重点确认 `--max-new-tokens` 默认是 `4000`、`--with-mtp` 是 flag 型（无取值）。

2. **预测基准模式的 MTP 行为**：在不看答案的情况下预测——执行

   ```bash
   python -m tilert.generate --model deepseek_v3_2 \
       --model-weights-dir /path/to/DeepSeek-V3.2-TileRT \
       --max-new-tokens 100 --workloads short
   ```

   时，**即使没有写 `--with-mtp`**，生成器会以 `with_mtp=True` 还是 `False` 加载？基准表里会出现哪几种模式？

3. **实际验证**：在有 GPU 与权重的环境运行上述命令（需要 u1-l3 的环境与 u1-l6 的转换权重）。对照 [tilert/generate.py:237-248](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L237-L248) 与 [tilert/generate.py:266-271](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L266-L271)，确认：
   - `--workloads short` 让 `allowed_workloads = {"short"}`，只跑 `short_bench.run`，跳过 coding/long；
   - 汇总表会出现「top-k1 w/o MTP」「top-k1 w/ MTP」「top-p0.95 w/ MTP」三行（若未用 `--modes` 过滤）。

**需要观察的现象**：

- `--max-new-tokens 100` 限制每次生成最多 100 token，基准跑得比默认 4000 快得多。
- 即便命令行没写 `--with-mtp`，汇总表里仍会出现「w/ MTP」的行——这正是 4.4 那个反直觉点的实证。

**预期结果**：第 2 步的预测应当是「`with_mtp=True`，出现三种模式」；第 3 步的实际运行应与预测一致。

**待本地验证**：第 3 步需要真实 B200 环境与转换好的权重。若无 GPU，可退化为源码阅读型实践——只做第 1、2 步，并用人脑跟踪 [generate.py:192-248](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L192-L248) 的执行路径来「验证」预测。

## 6. 本讲小结

- `python -m tilert.generate` 的入口是 `parse_args()`，全部 16 个参数都在 [generate.py:75-150](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L75-L150) 用 argparse 定义；命令行短横线 flag 解析后变成下划线属性名。
- 权重目录由 `get_weights_dir(model, cli_override)` 解析，优先级是「`--model-weights-dir` > `~/.tilert/config.toml` 的 `[weights][model]`」，配置缺失/语法错/键缺失都有友好的引导式报错。
- `get_generator(model_type, ...)` 是模型分发枢纽：先 `tilert.load_backend(model_type)` 加载后端（u1-l3 的懒加载入口），再延迟 import 对应生成器类并构造；两个模型族接收同一套参数名。
- `use_topp` 不是独立开关，由 `top_p < 1.0` 隐式推导；默认 `--top-p 1.0` 意味着默认走 top-k 而非 top-p。
- **反直觉点**：基准模式下 `--with-mtp` 被忽略，生成器强制以 `with_mtp=True` 加载——因为基准套件本身要同时测 w/ 与 w/o MTP 两种模式，必须先备好 MTP 权重。
- `__main__` 的完整链路是：解析参数 → 解析权重目录 → 决定 with_mtp → 构造生成器 → 加载权重 → 跑交互或基准 → `cleanup()` 收尾。

## 7. 下一步学习建议

- **下一篇 u1-l5（程序化 API 与 Generator 生命周期）**：本讲把生成器当黑盒，只看到 `from_pretrained / generate / cleanup` 被 `__main__` 调用。u1-l5 会打开这个黑盒，讲清 `DSAv32Generator` / `GLM5Generator` 的构造参数、完整生命周期、`generate` 返回的 `(text, time_list, accepted_counts, prompt_len)` 结构，以及如何脱离 CLI 用纯 Python API 完成一次生成。
- **u1-l6（权重转换）**：如果想理解 `--model-weights-dir` 指向的那套 per-device 分片权重是怎么从 HF checkpoint 变来的，读 u1-l6。
- **u3-l2（生成主循环）**：本讲的「交互模式 `generator.generate(prompt)`」内部其实是逐 token 解码循环，那套循环的源码留到 u3-l2 精读。
- **建议阅读源码**：先重读 [tilert/generate.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py) 全文（只有 300 行），再扫一眼 [tilert/benchmark/__init__.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py) 里的 `BenchMode` 与 `apply_mode`，为 u1-l5 和 u3-l5 打基础。
