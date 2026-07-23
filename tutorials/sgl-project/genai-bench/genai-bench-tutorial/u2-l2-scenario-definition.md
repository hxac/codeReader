# Traffic Scenario 场景定义与解析

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 genai-bench 中各种场景字符串（`N`/`D`/`U`/`E`/`R`/`I`/`A`/`dataset`）的语法含义，并说出它们的正则校验规则。
- 理解 `Scenario` 抽象基类如何通过 `__init_subclass__` 在「定义子类」的瞬间自动完成注册，从而构建出一个「字符串前缀 → 子类」的查找表。
- 独立用 `Scenario.from_string(...)` 把任意一个场景字符串解析成对象，并调用 `sample()` 得到采样结果。

## 2. 前置知识

在进入本讲前，建议你已经掌握：

- **任务（task）字符串**：上一讲（u2-l1）讲过，genai-bench 用 `text-to-text`、`image-text-to-text`、`text-to-embeddings` 这样的 `<input>-to-<output>` 字符串描述一次基准测试的输入/输出模态。任务决定了「用哪种采样器」「构造哪种请求」。
- **场景（scenario）是什么**：如果说 **任务** 决定「打什么模态的请求」，那么 **场景** 决定「每个请求的输入/输出规模长什么样」。例如同样是 `text-to-text`，`D(100,1000)` 表示「固定 100 个输入 token、1000 个输出 token」，而 `N(480,240)/(300,150)` 表示「输入/输出 token 数都服从正态分布」。场景是采样器（Sampler，下一讲 u2-l4）构造请求时的「规格说明书」。
- **Python 类与 `__init_subclass__`**：当一个类被定义时，Python 会自动调用其父类的 `__init_subclass__` 钩子。本讲的核心机制正是利用它来自动注册子类。
- **正则表达式基础**：能看懂 `\d+`、`(...)?`、`\/` 这类写法即可。

一句话定位：**任务负责「定性」，场景负责「定量」。**

## 3. 本讲源码地图

本讲涉及的关键文件都在 `genai_bench/scenarios/` 子包内：

| 文件 | 作用 |
| --- | --- |
| [genai_bench/scenarios/base.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py) | 定义场景类型枚举、`Scenario` 抽象基类、注册表 `_registry`、`from_string`/`validate` 工厂，以及通用参数解析函数 `parse_params_str` 和特殊场景 `DatasetScenario`。 |
| [genai_bench/scenarios/text.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/text.py) | 文本类场景实现：正态 `NormalDistribution`、均匀 `UniformDistribution`、确定性 `DeterministicDistribution`，以及嵌入 `EmbeddingScenario`、重排 `ReRankScenario`。 |
| [genai_bench/scenarios/multimodal.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/multimodal.py) | 多模态场景实现：图像 `ImageModality`、音频 `AudioModality`。 |
| [docs/user-guide/scenario-definition.md](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/user-guide/scenario-definition.md) | 官方用户文档，列出所有场景的语法与示例，是「人读」的规格表。 |

此外，下文会顺带引用两处「场景被消费」的入口，帮助你看清场景在整个项目里的位置：

- [genai_bench/cli/validation.py:142-169](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L142-L169)：CLI 收到 `--traffic-scenario` 后用 `Scenario.validate` 做校验。
- [genai_bench/distributed/runner.py:329-338](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L329-L338)：分布式运行时，master 把场景字符串下发给 worker，worker 用 `Scenario.from_string` 还原成对象。

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：**4.1 场景语法与正则**（解决「字符串怎么写、怎么校验」）、**4.2 分布类与模态类实现**（解决「每个场景类内部怎么采样」）、**4.3 注册表与 from_string 工厂**（解决「字符串如何自动路由到正确的类」）。

### 4.1 场景语法与正则

#### 4.1.1 概念说明

genai-bench 用一种非常紧凑的「微型语言」来描述场景。每条场景字符串的结构都是：

```
<类型前缀>(<参数>) [/(<参数>)]
```

- **类型前缀**：一个大写字母（或 `dataset` 这个特殊单词），决定场景的种类。
- **参数**：一对圆括号包着的、逗号分隔的整数，含义随前缀而变。
- **斜杠 `/`**：仅文本分布类场景需要，用来同时描述「输入」和「输出」两组参数。

官方文档把所有合法语法列成了一张表，见 [docs/user-guide/scenario-definition.md:17-46](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/user-guide/scenario-definition.md#L17-L46)。汇总如下：

| 前缀 | 类名 | 语法 | 示例 | `sample()` 返回含义 |
| --- | --- | --- | --- | --- |
| `N` | NormalDistribution | `N(mean_in,stddev_in)/(mean_out,stddev_out)` | `N(480,240)/(300,150)` | `(num_input_tokens, num_output_tokens)`，各按正态分布采样 |
| `D` | DeterministicDistribution | `D(num_in,num_out)` | `D(100,1000)` | `(num_input_tokens, num_output_tokens)`，恒定常数 |
| `U` | UniformDistribution | `U(min_in,max_in)/(min_out,max_out)` 或 `U(max_in,max_out)` | `U(50,100)/(200,250)` | `(num_input_tokens, num_output_tokens)`，按均匀分布采样 |
| `E` | EmbeddingScenario | `E(tokens_per_document)` | `E(1024)` | `tokens_per_document`（单个 int） |
| `R` | ReRankScenario | `R(tokens_per_document,tokens_per_query)` | `R(1024,100)` | `(tokens_per_document, tokens_per_query)` |
| `A` | AudioModality | `A(num_input_chars)` | `A(500)` | `num_input_chars`（TTS 输入字符数） |
| `I` | ImageModality | `I(width,height)` 或 `I(width,height,num_images)` | `I(512,512)`、`I(2048,2048,2)` | `((width, height), num_images, max_output_token)` |
| `dataset` | DatasetScenario | `dataset` | `dataset` | 不采样（直接用数据集原文，调用会抛 `NotImplementedError`） |

#### 4.1.2 核心流程

每条场景字符串在被使用前都要过一遍**正则校验**：

1. 取出开头的字母/单词作为「类型前缀」（如 `N(480,240)/(300,150)` → 前缀 `N`）。
2. 用前缀去注册表里查到对应的类（查不到 → 报「未知类型」错）。
3. 用该类自带的 `validation_pattern` 正则去整条匹配（不匹配 → 报「格式不符」错）。

校验是「类型 + 格式」双重把关：类型错（如 `X(...)`）和格式错（如缺斜杠 `N(10,20),(30,40)`、参数带空格 `I(1024, 1024)`）会被区分报错。

#### 4.1.3 源码精读

每个场景类都用一个类属性 `validation_pattern` 声明自己的正则。例如确定性分布要求严格两段整数、无斜杠：

正态分布必须有斜杠分隔的「输入/输出」两组（[genai_bench/scenarios/text.py:22](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/text.py#L22)）：`r"^N\(\d+,\d+\)\/\(\d+,\d+\)$"`。

均匀分布的斜杠段是可选的，所以正则用 `(?:\/\(\d+,\d+\))?`（[genai_bench/scenarios/text.py:75](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/text.py#L75)）：

```python
validation_pattern = r"^U\(\d+,\d+\)(?:\/\(\d+,\d+\))?$"
```

图像模态第三个数字（图片数量）可选，用 `(?:,\d+)?`（[genai_bench/scenarios/multimodal.py:42](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/multimodal.py#L42)）：`r"^I\(\d+,\d+(?:,\d+)?\)$"`。

特殊场景 `dataset` 用整串精确匹配（[genai_bench/scenarios/base.py:140](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L140)）：`r"^dataset$"`。

`validate` 方法集中执行「查表 + 正则匹配」这两步（[genai_bench/scenarios/base.py:109-129](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L109-L129)）：先用 `re.match(r"^([A-Za-z]+)", scenario_str)` 取出前缀，再到 `_registry` 查类；查不到就抛 `ValueError` 并列出所有受支持类型，查到了就用该类的 `validation_pattern` 复验。

CLI 入口正是复用这个 `validate` 来校验用户输入的 `--traffic-scenario`（[genai_bench/cli/validation.py:142-148](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L142-L148)），把 `ValueError` 包成 click 的 `BadParameter`，给出友好报错。

#### 4.1.4 代码实践

**实践目标**：亲手触发几条「格式不符」的报错，理解正则的边界。

**操作步骤**（在仓库根目录用 `python -c` 即可，无需运行真实压测）：

```python
# 示例代码
from genai_bench.scenarios.base import Scenario

# 1) 正常：应返回 True
print(Scenario.validate("D(100,1000)"))

# 2) 故意写错，逐个观察报错信息：
for bad in ["X(10,20)/(30,40)",   # 未知前缀
            "N(10,20),(30,40)",   # 缺少斜杠
            "I(1024, 1024)",      # 参数里多了空格
            "E1024"]:             # 缺括号
    try:
        Scenario.validate(bad)
    except ValueError as e:
        print(f"[{bad}] -> {e}")
```

**需要观察的现象**：第 1 条打印 `True`；后四条分别抛 `ValueError`，且错误信息会区分「未知类型」和「格式不符」两种。

**预期结果**：`X(...)` 报 `Unknown type 'X'`；其余几条报 `Expected to match pattern: ...`。

> 待本地验证：实际错误文案以你本地运行输出为准。

#### 4.1.5 小练习与答案

**练习 1**：写出能同时通过 `U(50,100)` 和 `U(50,100)/(200,250)` 两种写法的正则设计要点。

**参考答案**：把「斜杠 + 第二组参数」设为可选段，即 `(?:\/\(\d+,\d+\))?`；这正是 `UniformDistribution.validation_pattern` 的做法（text.py:75）。

**练习 2**：为什么 `I(1024, 1024)`（中间有空格）会被判非法？

**参考答案**：`ImageModality` 的正则是 `^I\(\d+,\d+(?:,\d+)?\)$`，逗号后紧跟 `\d+`，不允许多余空格；空格不匹配 `\d`，于是整串校验失败。

---

### 4.2 分布类与模态类实现

#### 4.2.1 概念说明

校验只回答「字符串合不合法」，而**采样**才回答「这条场景具体要多少 token」。每个场景类都实现了 `sample()`，把「参数」变成「一个具体请求的规模」。

场景类可以分成三类：

- **文本分布类**（`N`/`D`/`U`）：返回 `(num_input_tokens, num_output_tokens)`，是带随机性的（`D` 除外）。
- **模态类**（`I`/`A`）：返回图像尺寸/字符数等非 token 的「规模描述」。
- **特殊类**（`dataset`）：不参与 token 整形，`sample()` 直接抛 `NotImplementedError`，告诉采样器「走数据集原文模式」。

#### 4.2.2 核心流程

以正态分布为例，`sample()` 的逻辑是「采样 + 截断下界」：

1. 用 `numpy` 从正态分布抽一个样本，转成整数。
2. 用 `max(...)` 给一个最小值兜底：输入至少 1 个 token，输出至少 2 个 token。

写成公式（`⌊·⌋` 为取整，\(\mu\) 为均值、\(\sigma\) 为标准差）：

\[ \text{num\_input\_tokens} = \max\!\left(1,\; \left\lfloor X_{\text{in}} \right\rfloor\right),\quad X_{\text{in}} \sim \mathcal{N}(\mu_{\text{in}}, \sigma_{\text{in}}^{2}) \]

\[ \text{num\_output\_tokens} = \max\!\left(2,\; \left\lfloor X_{\text{out}} \right\rfloor\right),\quad X_{\text{out}} \sim \mathcal{N}(\mu_{\text{out}}, \sigma_{\text{out}}^{2}) \]

确定性分布则没有随机性，`sample()` 直接返回构造时存好的两个常数。图像模态返回一个三元组，结构上和文本类不同，所以采样器会按「返回几个值、是什么含义」分别处理。

#### 4.2.3 源码精读

正态分布的 `sample()`（[genai_bench/scenarios/text.py:36-45](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/text.py#L36-L45)）：

```python
def sample(self) -> Tuple[int, int]:
    num_input_tokens = max(1, int(np.random.normal(
        self.mean_input_tokens, self.stddev_input_tokens)))
    num_output_tokens = max(2, int(np.random.normal(
        self.mean_output_tokens, self.stddev_output_tokens)))
    return num_input_tokens, num_output_tokens
```

确定性分布最简单，直接返回常数（[genai_bench/scenarios/text.py:145-146](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/text.py#L145-L146)）：

```python
def sample(self) -> Tuple[int, int]:
    return self.num_input_tokens, self.num_output_tokens
```

均匀分布用 `np.random.uniform`，并支持「只给上界」的简写（下界 `or 1` 兜底，[genai_bench/scenarios/text.py:89-98](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/text.py#L89-L98)）。

图像模态的 `sample()` 返回三元组（尺寸、图片数、可选输出上限），结构与文本类完全不同（[genai_bench/scenarios/multimodal.py:56-64](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/multimodal.py#L56-L64)）：

```python
def sample(self) -> Tuple[Tuple[int, int], int, int | None]:
    return (
        (self.num_input_dimension_width, self.num_input_dimension_height),
        self.num_input_images,
        self.max_output_token,
    )
```

特殊场景 `DatasetScenario` 的 `sample()` 故意不实现，用来「占位 + 报错」（[genai_bench/scenarios/base.py:142-146](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L142-L146)）：采样器在 `dataset` 模式下会绕过 token 整形、直接取数据集原文。

#### 4.2.4 代码实践

**实践目标**：直接构造场景对象、观察 `sample()` 的返回值与随机性。

**操作步骤**：

```python
# 示例代码
from genai_bench.scenarios.text import NormalDistribution, DeterministicDistribution
from genai_bench.scenarios.multimodal import ImageModality

# 正态：每次调用结果都不同（围绕均值波动）
n = NormalDistribution(480, 240, 300, 150)
print([n.sample() for _ in range(3)])

# 确定性：永远恒定
d = DeterministicDistribution(100, 1000)
print([d.sample() for _ in range(3)])

# 图像：返回三元组
i = ImageModality(1024, 1024)
print(i.sample())   # ((1024, 1024), 1, None)
```

**需要观察的现象**：正态分布每次返回的 `(输入, 输出)` 都在均值附近波动，但输入 ≥ 1、输出 ≥ 2；确定性分布三次完全相同；图像返回 `((1024, 1024), 1, None)`。

**预期结果**：与上述注释一致。

> 待本地验证：正态分布的具体数值是随机的，只需确认范围与下界约束。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `NormalDistribution.sample()` 要给输出 token 设 `max(2, ...)` 而不是 `max(1, ...)`？

**参考答案**：输出 0 或 1 个 token 对「流式输出」基准没有意义（无法计算 TPOT 等每 token 指标），代码用 `max(2, ...)` 保证每次至少有 2 个输出 token，避免出现退化样本（见 text.py:43）。

**练习 2**：`ImageModality(100, 200, max_output_token=1000).sample()` 返回什么？

**参考答案**：`((100, 200), 1, 1000)`。这与测试 `test_scenario_sample_image` 的断言一致（见 [tests/scenarios/test_base.py:159-166](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/scenarios/test_base.py#L159-L166)）。

---

### 4.3 注册表与 from_string 工厂

#### 4.3.1 概念说明

你可能会问：`from_string("D(100,1000)")` 怎么知道要构造 `DeterministicDistribution`？答案是一个**注册表（registry）**——一个「前缀字母 → 场景类」的字典 `_registry`。

这里最巧妙的设计是：**注册是自动的**。你不需要手写 `register("D", DeterministicDistribution)`，只要定义一个带 `scenario_type` 的子类，Python 的 `__init_subclass__` 钩子就会立刻把它登记进表里。这种「定义即注册」的模式让新增场景几乎零成本（这也是 u8-l3 扩展指南的基础）。

#### 4.3.2 核心流程

`from_string` 的解析流程可以拆成 5 步：

```
"N(480,240)/(300,150)"
   │
   ① 取前缀：re.match(r"^([A-Za-z]+)")  →  "N"
   ② 校验：Scenario.validate(整串)        →  查表 + 正则
   ③ 查表：_registry["N"]                  →  NormalDistribution
   ④ 裁参数：scenario_str 去掉前缀 "N"     →  "(480,240)/(300,150)"
   ⑤ 委派：NormalDistribution.parse("(480,240)/(300,150)")
                                              │
                            parse_params_str 拆出 [(480,240),(300,150)]
                                              │
                            NormalDistribution(480, 240, 300, 150)
```

关键细节有两个：

- **前缀是多字符的**：`dataset` 这种单词也是合法前缀，所以用 `[A-Za-z]+`（而非单字母）来取前缀，再用「去掉前缀长度」的方式裁出参数串（[genai_bench/scenarios/base.py:106](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L106)）。
- **注册在「导入模块」时发生**：`_registry` 只有在对应子类被定义（即其所在模块被执行）后才会有内容。`scenarios/__init__.py` 显式 import 了各子类，正是为了触发注册。

#### 4.3.3 源码精读

注册表与自动注册（[genai_bench/scenarios/base.py:55-67](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L55-L67)）：

```python
_registry: Dict[str, Type["Scenario"]] = {}
scenario_type: (...)          # 子类必须声明，如 TextDistribution.NORMAL
validation_pattern: str       # 子类必须声明

def __init_subclass__(cls, **kwargs):
    super().__init_subclass__(**kwargs)
    cls._registry[cls.scenario_type.value] = cls
```

每个子类都声明了 `scenario_type`，其 `.value` 就是注册键。例如 `DeterministicDistribution` 声明 `scenario_type = TextDistribution.DETERMINISTIC`，而 `TextDistribution.DETERMINISTIC.value == "D"`（[genai_bench/scenarios/base.py:12-13](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L12-L13)），于是注册键就是 `"D"`。

`from_string` 工厂方法（[genai_bench/scenarios/base.py:90-107](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L90-L107)）：

```python
@classmethod
def from_string(cls, scenario_str: str) -> "Scenario":
    match = re.match(r"^([A-Za-z]+)", scenario_str)
    type_token = match.group(1) if match else scenario_str[0]
    cls.validate(scenario_str)
    scenario_class = cls._registry.get(type_token)
    params_str = scenario_str[len(type_token):]
    return scenario_class.parse(params_str)
```

通用参数解析 `parse_params_str`（[genai_bench/scenarios/base.py:156-172](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L156-L172)）：按 `/` 拆分多组，每组去掉首尾的括号、再按逗号拆成整数元组。例如 `"(480,240)/(300,150)"` → `[(480, 240), (300, 150)]`。各子类的 `parse` 再把这些元组「解包」成自己的构造参数（如 [genai_bench/scenarios/text.py:53-63](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/text.py#L53-L63)）。

**一个值得注意的边界**：枚举里声明了 `VIDEO = "V"`（[genai_bench/scenarios/base.py:39](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L39)），但项目里并没有 `VideoModality` 子类，因此 `_registry` 里没有 `"V"` 这个键——`from_string("V(...)")` 会因为 `validate` 查表失败而抛 `ValueError: Unknown type 'V'`。这正好说明：**注册表是由「实际存在的子类」决定的，而不是由枚举决定的**。

真实消费链路：分布式运行时，master 把场景字符串作为消息下发给 worker，worker 端用 `Scenario.from_string(msg.data)` 把字符串还原成对象并挂到 environment 上（[genai_bench/distributed/runner.py:333](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L333)）。也就是说，**场景字符串是跨进程传递的「序列化形态」**，`from_string`/`to_string` 就是它的编码/解码器。

#### 4.3.4 代码实践

**实践目标**：跟踪 `from_string` 的完整路由，并验证「定义即注册」。

**操作步骤**：

```python
# 示例代码
from genai_bench.scenarios.base import Scenario, parse_params_str

# 1) 观察 parse_params_str 的拆分结果
print(parse_params_str("(480,240)/(300,150)"))   # [(480, 240), (300, 150)]

# 2) 观察注册表内容（键 = 前缀，值 = 类）
print(sorted(Scenario._registry.keys()))         # ['A','D','E','I','N','R','U','dataset']

# 3) 跟踪路由：同一个入口，解析出不同类
for s in ["N(480,240)/(300,150)", "E(1024)", "I(1024,1024)", "dataset"]:
    obj = Scenario.from_string(s)
    print(f"{s:24s} -> {type(obj).__name__}")
```

**需要观察的现象**：注册表的键恰好是各子类 `scenario_type.value` 的集合，且不含 `"V"`；四条字符串分别被解析成 `NormalDistribution`、`EmbeddingScenario`、`ImageModality`、`DatasetScenario`。

**预期结果**：与注释一致。注意 `V` 不在注册表中（无对应子类）。

> 待本地验证：注册表键的顺序以本地输出为准（此处已用 `sorted` 排序）。

#### 4.3.5 小练习与答案

**练习 1**：若想新增一个视频场景 `V(width,height)`，最少需要做哪些事？

**参考答案**：定义一个 `VideoModality(Scenario)` 子类，声明 `scenario_type = MultiModality.VIDEO` 和 `validation_pattern = r"^V\(\d+,\d+\)$"`，并实现 `sample/to_string/parse`。由于 `__init_subclass__` 会自动注册，定义完成（且模块被 import）后 `from_string("V(...)")` 即可生效——无需手动改 `_registry`。

**练习 2**：`Scenario.from_string("E(1024,100)")` 会发生什么？

**参考答案**：前缀 `E` 能在注册表查到 `EmbeddingScenario`，但它的 `validation_pattern` 是 `^E\(\d+\)$`，只允许一个参数；`E(1024,100)` 不匹配，于是 `validate` 抛 `ValueError: Invalid scenario string ... Expected to match pattern: ^E\(\d+\)$`。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性任务：**写一个脚本，解析三种不同形态的场景字符串，分别调用 `sample()`，并对其中可逆的场景做「字符串 → 对象 → 字符串」的往返验证。**

**实践目标**：亲手验证「语法校验（4.1）+ 采样实现（4.2）+ 工厂路由（4.3）」三者如何协同工作。

**操作步骤**（在仓库根目录创建 `parse_and_sample.py` 后运行 `python parse_and_sample.py`）：

```python
# 示例代码：parse_and_sample.py
from genai_bench.scenarios import Scenario

cases = ["N(480,240)/(300,150)", "E(1024)", "I(1024,1024)"]

for s in cases:
    scenario = Scenario.from_string(s)
    result = scenario.sample()
    print(f"{s:26s} -> 类型={type(scenario).__name__:<24} sample()={result}")

# 往返验证：对确定性场景，from_string -> to_string 应能还原
from genai_bench.scenarios.text import DeterministicDistribution
d = DeterministicDistribution.from_string("D(100,1000)")
assert d.to_string() == "D(100,1000)", "往返不一致"
print("往返验证通过：", d.to_string())
```

**需要观察的现象**：

1. 三条字符串分别被路由到 `NormalDistribution`、`EmbeddingScenario`、`ImageModality`。
2. `N(...)` 的 `sample()` 是 `(输入, 输出)` 两个随机整数（输入 ≥ 1，输出 ≥ 2）；`E(1024)` 的 `sample()` 是单个整数 `1024`；`I(1024,1024)` 的 `sample()` 是三元组 `((1024, 1024), 1, None)`。
3. 确定性场景的 `to_string()` 能还原原字符串。

**预期结果**：输出形如

```
N(480,240)/(300,150)       -> 类型=NormalDistribution      sample()=(xxx, yyy)
E(1024)                    -> 类型=EmbeddingScenario        sample()=1024
I(1024,1024)               -> 类型=ImageModality            sample()=((1024, 1024), 1, None)
往返验证通过： D(100,1000)
```

其中 `xxx, yyy` 是正态采样的随机值。你也可以参考项目自带的测试 [tests/scenarios/test_base.py:14-74](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/scenarios/test_base.py#L14-L74)，里面 `test_scenario_from_string_*` 系列断言与上面的解析结果一致。

> 待本地验证：`N(...)` 的采样值是随机的，只需确认类型与下界约束。

## 6. 本讲小结

- 场景字符串是一条微型语言：`<前缀>(参数)[/(参数)]`，前缀决定种类、参数决定规模；`dataset` 是不带参数的特殊形态。
- 每个场景类用 `validation_pattern` 自带正则，`Scenario.validate` 做「查表 + 正则」双重校验，能区分「未知类型」和「格式不符」。
- `sample()` 是「参数 → 具体规模」的转换：文本分布类返回 `(输入, 输出)` 且带 `max` 下界兜底，模态类返回尺寸/字符数等结构化结果，`DatasetScenario` 不采样。
- 注册表 `_registry` 由 `__init_subclass__` 在「定义子类」时自动填充，注册键就是 `scenario_type.value`；枚举里有 `V` 但没有子类，故 `V(...)` 会被判非法——注册表只认实际存在的子类。
- `from_string` = 取前缀 → 校验 → 查表 → 裁参数 → 委派 `parse`；`to_string` 是它的逆运算，二者共同构成场景跨进程传递的「序列化协议」。

## 7. 下一步学习建议

本讲解决了「场景字符串如何描述与解析」，但还没有讲「场景如何驱动真实请求的构造」。建议继续：

- **u2-l3 数据集加载**：了解 `dataset` 模式下，采样器如何从数据集取原文，以及 `DatasetConfig`/`DataLoaderFactory` 的分工。
- **u2-l4 采样器与请求构造**：这是本讲的直接下游——`Sampler` 接收 `Scenario` 对象，调用 `scenario.sample()` 拿到规模，再据此填充 `UserChatRequest` 等协议模型（呼应 u1-l5）。重点看 `TextSampler.sample` 如何按 `output_modality` 分发。
- 若对扩展机制感兴趣，可先翻阅 `docs/developer-guide/adding-new-features.md`，再到 u8-l3 系统学习「如何新增一个场景/采样器/后端」。
