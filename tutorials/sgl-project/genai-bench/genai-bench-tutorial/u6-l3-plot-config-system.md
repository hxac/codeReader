# 绘图配置系统 plot_config

## 1. 本讲目标

本讲聚焦 genai-bench 的「声明式绘图配置系统」`genai_bench/analysis/plot_config.py`。学完本讲，你应当能够：

- 看懂用 Pydantic 定义的 `YFieldSpec` / `PlotSpec` / `PlotLayout` / `PlotConfig` 四层配置模型，以及它们自带的校验规则。
- 区分单线（`y_field`）与多线（`y_fields`）两种画法，理解二者「互斥且至少一个」的约束。
- 掌握内置 preset（如 `2x4_default`、`simple_2x2`）的用法与配置加载的分发逻辑（`load_config` / `load_preset` / `load_from_file`）。
- 理解 `get_field_value` 如何用 `stats.ttft.mean` 这样的点分路径，从一个 `AggregatedMetrics` 对象里取到具体数值——这正是「声明式配置」与「真实数据」之间的桥梁。

本讲只讲**配置本身**（数据结构、校验、preset、路径解析），**不**讲最终如何用 matplotlib 把图画出来（那是下一讲 u6-l4「灵活绘图与 plot 命令」的内容）。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 什么是「声明式配置」

如果让你写一个画图程序，最直接的做法是写一段命令式代码：「取并发数组，取 TTFT 数组，调 `plot(x, y)`」。这很灵活，但**每改一种图都要改代码**。

声明式（declarative）配置换了个思路：把「要画什么」写进一份 JSON（标题、X 轴字段、Y 轴字段、网格位置……），画图引擎读取这份描述去执行。于是：

- 换图 = 改 JSON，不用碰代码、不用重新部署。
- 同一份引擎可以服务无数种图。
- 配置可以保存、分享、版本管理。

`plot_config.py` 就是 genai-bench 绘图系统的「配置语言定义」：它规定了一份合法的画图 JSON 长什么样。

### 2.2 点分路径：把字符串翻译成对象属性

真实的指标数据是一个嵌套的 Pydantic 对象 `AggregatedMetrics`（见 u6-l1、u4-l3）。一个 `mean`（均值）TTFT 值，在对象里是这么层层访问的：

```text
metrics.stats.ttft.mean
        │     │    │
        │     │    └─ 统计量：mean / p99 / max ...
        │     └─ 指标名：ttft / e2e_latency / tpot ...
        └─ 统计容器
```

但配置 JSON 里不能写 Python 表达式，只能写字符串。于是系统约定：用点号 `.` 把这条访问路径写成字符串 `"stats.ttft.mean"`，再由一个解析函数把它「翻译」回连续的 `getattr` 调用。这个翻译函数就是本讲要讲的 `get_field_value`。

> 提示：本讲承接 u6-l1 的实验加载结果（`scenario → concurrency → AggregatedMetrics` 网格）与 u4-l3 的指标模型分层。如果你还不清楚 `AggregatedMetrics.stats.ttft` 是什么，建议先回顾 u4-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [genai_bench/analysis/plot_config.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L1-L727) | 本讲主角：四层配置模型 + `PlotConfigManager`（preset、加载、字段路径解析） |
| [examples/plot_configs/custom_2x2.json](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/examples/plot_configs/custom_2x2.json#L1-L45) | 一个最小可用的 2×2 单线配置示例 |
| [examples/plot_configs/multi_line_latency.json](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/examples/plot_configs/multi_line_latency.json#L1-L85) | 演示多线（`y_fields`）+ 对数坐标的示例 |
| [genai_bench/time_units.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py#L1-L164) | `TimeUnitConverter.get_unit_label`：配置加载时改写时间单位标签 |
| [genai_bench/analysis/flexible_plot_report.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L1-L937) | 配置的**消费者**（下一讲主讲），本讲只引用它调用 `get_field_value` 的地方 |
| [genai_bench/cli/report.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L1-L306) | `plot` 子命令：把 `--plot-config` / `--preset` 串到加载入口 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 配置模型与校验**、**4.2 preset 体系与配置加载**、**4.3 字段路径解析**。

### 4.1 配置模型与校验

#### 4.1.1 概念说明

一份画图配置在内存里是一个 `PlotConfig` 对象，它的结构是：

```text
PlotConfig
├── layout: PlotLayout        # 网格规模（几行几列、画布尺寸）
└── plots: List[PlotSpec]     # 一张张子图的规格
        ├── title             # 子图标题
        ├── x_field           # X 轴字段路径（字符串）
        ├── y_field           # Y 轴单字段（二选一）
        ├── y_fields           # Y 轴多字段（二选一），每项是 YFieldSpec
        ├── x_label / y_label # 自定义坐标轴标签
        ├── plot_type         # line / scatter / bar
        ├── position          # (row, col) 子图在网格里的位置
        └── y_scale           # linear / log（可选）
```

设计上有两个关键选择：

1. **Y 轴用两种字段表达**：`y_field`（单线，直接给一个路径字符串）和 `y_fields`（多线，给一个 `YFieldSpec` 列表，每条线可单独配色、线型、图例）。多线用于「把 mean / p90 / p99 放在同一张图上对比」这类需求。
2. **校验内建到模型里**：用 Pydantic 的 `field_validator` 把约束焊在类定义上——非法配置在「构造对象」时就被拦下，根本不会流入绘图引擎。这就是「构造即校验」（见 u1-l5）的又一实例。

#### 4.1.2 核心流程

构造一个 `PlotConfig` 时，Pydantic 会按字段定义顺序依次校验，关键校验关卡如下：

```text
PlotSpec 校验
  ├─ plot_type ∈ {line, scatter, bar}           # 否则 ValueError
  ├─ y_scale ∈ {linear, log} 或 None            # 否则 ValueError
  └─ y_field 与 y_fields 互斥且至少一个           # 关键约束
        ├─ 两者都给          → "Cannot specify both"
        └─ 两者都没给(或空)  → "Must specify either"

PlotLayout 校验
  └─ rows ∈ [1,5]，cols ∈ [1,6]                  # 超界 ValueError

PlotConfig 校验
  └─ 每个 plot.position:
        ├─ row ≤ rows-1 且 col ≤ cols-1          # 不能越出网格
        └─ 位置不重复                             # 不能两个子图占同一格
```

一个值得注意的细节：`PlotSpec.position` 与 `PlotConfig.layout` 是**分离**的——子图自己声明它想落在第几行第几列，而网格有多大由 `layout` 决定；`PlotConfig` 层的校验负责确保「子图想坐的位置，在网格里确实存在，且没被别人占了」。

#### 4.1.3 源码精读

**多线单线规格 `YFieldSpec`**：一条 Y 轴线条的描述，`field` 是路径字符串，其余样式都是可选。

[genai_bench/analysis/plot_config.py:16-24](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L16-L24) 定义了 `field`（必填）、`label` / `color` / `linestyle`（可选）四个字段。

**子图规格 `PlotSpec`** 与核心的「互斥校验」：

[genai_bench/analysis/plot_config.py:63-78](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L63-L78) 实现了 `y_field` 与 `y_fields` 的互斥约束。注意它从 `info.data.get("y_field")` 取「已经校验过的前序字段」——Pydantic v2 的 `field_validator` 默认在字段定义顺序之后运行，`y_field`（第 32 行）定义在 `y_fields`（第 35 行）之前，所以这里能拿到 `y_field` 的值。

互斥关系可用真值表概括：

| `y_field` | `y_fields` | 结果 |
| --- | --- | --- |
| 有值 | 有值 | ❌ 报错：Cannot specify both |
| 有值 | None | ✅ 单线 |
| None | 有值（≥1） | ✅ 多线 |
| None | None 或空 | ❌ 报错：Must specify either |

**两个辅助方法**统一了单线/多线的访问口径，下游绘图代码不必关心到底是哪种：

[genai_bench/analysis/plot_config.py:80-91](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L80-L91)：`get_y_field_specs()` 总是返回一个列表——单线时把 `y_field` 包成单元素列表返回；`is_multi_line()` 只有在 `y_fields` 非空且长度 > 1 时才为真（注意：`y_fields` 只给 1 条线，不算多线）。

**网格规格 `PlotLayout`**：用 `Field(ge=1, le=5)` / `Field(ge=1, le=6)` 直接把行列数钉死在合法区间。

[genai_bench/analysis/plot_config.py:94-101](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L94-L101) 限定 `rows ∈ [1,5]`、`cols ∈ [1,6]`，对应文档里说的「任意 NxM 网格，从 1×1 到 5×6」。

**顶层 `PlotConfig` 与位置校验**：

[genai_bench/analysis/plot_config.py:110-130](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L110-L130) 遍历每个子图，做两件事：检查 `position` 是否越出 `layout` 边界（`row > rows-1` 或 `col > cols-1`），以及用 `set()` 去重检测重复位置。开头有个保护 `if not hasattr(info, "data") or "layout" not in info.data: return v`——当 `layout` 字段尚未校验时跳过，避免误报。

#### 4.1.4 代码实践

**目标**：亲手触发一次校验失败，确认「构造即校验」真的生效。

**操作步骤**（假设已 `pip install genai-bench`，在仓库根目录运行）：

1. 进入 Python 解释器；
2. 故意构造一份非法配置：网格是 2×2，却把一张子图放到 `[2, 0]`（第 3 行，越界）；
3. 观察抛出的异常。

```python
# 示例代码（不是项目原有代码）
from genai_bench.analysis.plot_config import PlotConfig

bad = PlotConfig(
    layout={"rows": 2, "cols": 2},
    plots=[
        {"title": "越界图", "x_field": "num_concurrency",
         "y_field": "stats.ttft.mean", "position": [2, 0]}  # 第 3 行，但只有 0/1 两行
    ],
)
```

**需要观察的现象**：`PlotConfig(...)` 这一行**不会**成功返回，而是抛出 `ValidationError`，错误信息含 `exceeds layout bounds (1, 1)`（`max_row=rows-1=1`、`max_col=cols-1=1`）。

**预期结果**：异常被 Pydantic 在构造阶段拦下。把 `position` 改成 `[1, 0]` 后再构造即可成功，验证「修对就能过」。

> 待本地验证：不同 Pydantic 版本的错误文案细节可能略有差异，但 `exceeds layout bounds` 关键字应当一致。

#### 4.1.5 小练习与答案

**练习 1**：如果一份配置里某个 `PlotSpec` 同时写了 `y_field` 和 `y_fields`，会发生什么？

**参考答案**：构造 `PlotSpec` 时由 [validate_y_fields](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L63-L78) 拦下，抛出 `ValidationError`，信息为 `Cannot specify both y_field and y_fields`。

**练习 2**：`y_fields` 里只放 1 条线，`is_multi_line()` 返回什么？为什么这样设计？

**参考答案**：返回 `False`。因为 [is_multi_line](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L89-L91) 要求 `y_fields is not None and len(y_fields) > 1`。这样设计是为了让「只有一条线的多线写法」走单线渲染路径（例如自动对数坐标、单图例等行为），避免把 1 条线误当多线处理。

---

### 4.2 preset 体系与配置加载

#### 4.2.1 概念说明

光有数据模型还不够，用户要能「方便地拿到一份配置」。`PlotConfigManager` 提供了三类配置来源：

1. **内置 preset**：项目预先写好的几套常用配置，用名字（如 `2x4_default`）直接引用，开箱即用。
2. **自定义文件**：用户写一份 JSON，用 `--plot-config path.json` 指定。
3. **字典**：在 Python 里直接传 dict（多用于测试或程序内拼装）。

`PlotConfigManager` 是一个纯类方法的「门面（facade）」，把这些来源统一收敛成「输入某种来源 → 输出一个校验过的 `PlotConfig` 对象」。它还顺带做了一个横切处理：**时间单位转换**——加载时按 `metrics_time_unit` 把标签里的 `(s)` 改写成 `(ms)`（承接 u4-l3）。

#### 4.2.2 核心流程

加载的分发逻辑由 `load_config` 统一入口把控：

```text
load_config(config_source, metrics_time_unit)
  │
  ├─ config_source is None      → load_preset("2x4_default")        # 默认
  ├─ config_source is str:
  │     ├─ 在 PRESETS 里         → load_preset(name)                  # 是 preset 名
  │     └─ 否则                  → load_from_file(path)              # 当作文件路径
  ├─ config_source is dict       → apply_time_unit_conversion(...) → PlotConfig(**data)
  └─ 其它类型                    → ValueError
```

注意一个微妙点：**字符串既可能是 preset 名，也可能是文件路径**。`load_config` 先查 `PRESETS` 字典——命中就当 preset，没命中才当文件路径。而 `load_preset` 内部又会回调 `load_config(该preset的dict)`，从而复用「字典 → 时间单位转换 → 构造校验」这条公共链路。

时间单位转换在加载链路里的位置：

```text
原始 dict/JSON ──► apply_time_unit_conversion(unit) ──► PlotConfig(**转换后的dict) ──► 校验
                  （只改写标签文字，不动 field 路径）
```

#### 4.2.3 源码精读

**5 个内置 preset**：定义在类属性 `PRESETS` 字典里，每个 value 就是一份合法的配置 dict。

[genai_bench/analysis/plot_config.py:137-483](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L137-L483) 定义了全部 preset。其特点汇总如下：

| preset 名 | 网格 | 线型 | 典型用途 |
| --- | --- | --- | --- |
| `2x4_default` | 2×4 | 单线 | 8 张标准图，向后兼容默认行为 |
| `2x4_tts` | 2×4 | 单线 | 语音（TTS）任务，TTFT 显示为 TTFB |
| `simple_2x2` | 2×2 | 单线 | 快速看核心指标 |
| `multi_line_latency` | 2×2 | 多线+对数 | 在单张图里对比 mean/p90/p99 |
| `single_scenario_analysis` | 2×2 | 多线 | 单场景深度分析 |

例如 [`2x4_default`](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L138-L216) 的第一张图把 `x_field` 设为 `mean_output_throughput_tokens_per_s`、`y_field` 设为 `stats.output_inference_speed.mean`——这正是文档「Quick Start」里列出的「Output Inference Speed vs Output Throughput」那张图。而 [`multi_line_latency`](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L331-L411) 演示了 `y_fields`（多条 `YFieldSpec`，各带 `color`/`linestyle`）和 `y_scale: "log"` 的用法。

**统一加载入口 `load_config`**：

[genai_bench/analysis/plot_config.py:539-563](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L539-L563) 按 `config_source` 的类型三分：`None` → 默认 preset；`str` → 先查 `PRESETS`、未命中则当文件；`dict` → 先做时间单位转换再构造。

**从文件加载 `load_from_file`**：

[genai_bench/analysis/plot_config.py:576-593](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L576-L593) 读 JSON → `apply_time_unit_conversion` → `PlotConfig(**data)`。它把 `json.JSONDecodeError` 和其它异常都翻译成带文件名的 `ValueError`，对用户更友好。

**时间单位转换 `apply_time_unit_conversion`**：

[genai_bench/analysis/plot_config.py:485-537](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L485-L537) 遍历每个 plot，对 `title` / `x_label` / `y_label` / `y_fields[*].label` 调 `TimeUnitConverter.get_unit_label`。它只改**文字标签**（如 `TTFT (s)` → `TTFT (ms)`），完全不动 `x_field` / `y_field` 这些路径字符串——路径指向的是数据，单位换算数据值是绘图阶段（u6-l4）的事，配置层只负责把标签显示对。

> 关于 `get_unit_label` 的正则细节：见 [genai_bench/time_units.py:118-136](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py#L118-L136)，它用正则把 `(s)`/`(seconds)` 替换为 `(ms)`（或反向），只认 `s`/`ms` 两种单位，其它单位原样返回。其行为有测试覆盖，见 [tests/analysis/test_plot_config.py:4-33](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/test_plot_config.py#L4-L33)。

**CLI 侧的接线**：`plot` 子命令把 `--preset` / `--plot-config` 映射到上述方法。

[genai_bench/cli/report.py:235-243](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L235-L243)：`preset` 优先；其次 `plot_config` 文件；都没有则用 `2x4_default`。注意 CLI 用的是 `load_preset(preset)` 和 `load_from_file(plot_config)`，与 `load_config` 的内部分发一致。

#### 4.2.4 代码实践

**目标**：体验三类来源的加载，并用内置 preset 快速拿到一份配置。

**操作步骤**：

```python
# 示例代码
from genai_bench.analysis.plot_config import PlotConfigManager as M

# 1) 默认来源（None）→ 2x4_default
cfg_default = M.load_config(None)
print("默认 preset 子图数:", len(cfg_default.plots), "网格:", cfg_default.layout.rows, "x", cfg_default.layout.cols)

# 2) preset 名
cfg_simple = M.load_preset("simple_2x2")
print("simple_2x2 子图数:", len(cfg_simple.plots))

# 3) 字典 + 时间单位转换（把标签里的 (s) 改成 (ms)）
cfg_dict = M.load_config(
    {"plots": [{"title": "TTFT (s)", "x_field": "num_concurrency",
                "y_field": "stats.ttft.mean", "position": [0, 0]}]},
    metrics_time_unit="ms",
)
print("转换单位后标题:", cfg_dict.plots[0].title)   # 期望: TTFT (ms)
```

**需要观察的现象**：三段都能成功返回 `PlotConfig` 对象；第 3 段的标题从 `TTFT (s)` 变成 `TTFT (ms)`。

**预期结果**：默认 preset 有 8 张子图、2×4 网格；`simple_2x2` 有 4 张子图；字典加载后单位标签被改写。

> 待本地验证：若环境未装 matplotlib 不影响本步（本步只构造配置对象，不画图）。

#### 4.2.5 小练习与答案

**练习 1**：调用 `load_config("simple_2x2")` 时，`"simple_2x2"` 是被当作 preset 还是文件路径？依据是什么？

**参考答案**：当作 preset。因为 [load_config](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L548-L554) 对字符串先查 `PRESETS`，`simple_2x2` 命中，于是走 `load_preset`。只有不在 `PRESETS` 里的字符串才会被当文件路径。

**练习 2**：为什么 `apply_time_unit_conversion` 不去改 `y_field` 里的 `stats.ttft.mean`？

**参考答案**：`y_field` 是「数据路径」，指向要取的数值；时间单位转换要改的是「数值本身」（在绘图阶段由 `TimeUnitConverter.convert_value` 完成，见 u6-l4）。配置加载层只负责把**给用户看的文字标签**（标题、轴标签、图例）改写成正确单位，避免误导；路径字符串若被改，反而会导致 `get_field_value` 取不到值。

---

### 4.3 字段路径解析

#### 4.3.1 概念说明

配置里的 `x_field` / `y_field` 都是字符串，如 `"stats.ttft.mean"`、`"num_concurrency"`。但绘图时需要的是**具体数值**。本模块解决的就是这个「字符串 → 数值」的翻译问题，核心是三个互补的工具：

- `get_field_value`：给定一个 `AggregatedMetrics` 对象和一条点分路径，返回路径终点的值。
- `get_available_fields`：不依赖任何真实数据，直接从 `AggregatedMetrics` 的**模型 schema** 枚举出所有「理论上可用」的字段路径（静态全表）。
- `get_fields_from_data`：给定一个**真实的** `AggregatedMetrics` 实例，枚举出「实际有数据（非 None）」的字段及其值（动态子表）。

三者对应三种使用场景：绘图取值、写配置时的字段速查、CLI `--list-fields` 对真实数据可字段的展示。

#### 4.3.2 核心流程

`get_field_value` 的算法很朴素——按 `.` 切分路径，逐段 `getattr` 下钻：

```text
get_field_value(metrics, "stats.ttft.mean")
  parts = ["stats", "ttft", "mean"]
  value = metrics
  for part in parts:
      value = getattr(value, part)      # metrics → metrics.stats → .ttft → .mean
      若中途属性不存在 → raise AttributeError("Field path '...' not found")
  return value                            # 一个 float
```

两条路径形态的区别：

- **直连字段**（一段）：如 `num_concurrency`、`requests_per_second`、`error_rate`、`mean_output_throughput_tokens_per_s`、`run_duration`——它们是 `AggregatedMetrics` 的顶层属性（见 [metrics.py:144-175](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L144-L175)）。
- **统计字段**（三段 `stats.{指标}.{统计量}`）：如 `stats.ttft.mean`、`stats.e2e_latency.p99`——下钻到 `MetricStats` 容器里的某个 `StatField` 的某个统计量（见 u4-l3 的指标分层）。

`get_available_fields` 则用「笛卡尔积」生成所有统计字段路径：

\[
\text{可用统计路径集合} = \{\,\text{stats}.\text{metric}.\text{stat} \;\mid\; \text{metric} \in M,\ \text{stat} \in S\,\}
\]

其中 \(M \) 是 10 个指标名（ttft、tpot、e2e_latency…），\(S\) 是 11 个统计量（min、max、mean、p25…p99），合计 110 条 `stats.*.*` 路径，再加上若干直连字段。

#### 4.3.3 源码精读

**取值核心 `get_field_value`**：

[genai_bench/analysis/plot_config.py:714-726](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L714-L726) 用 `hasattr` 判断、`getattr` 下钻，找不到就抛 `AttributeError`。注意它**不区分**直连字段和统计字段——对 `"num_concurrency"`，`parts=["num_concurrency"]`，一次 `getattr` 就返回；对 `"stats.ttft.mean"`，三次下钻。统一逻辑，靠路径自身的形状区分。

**它被谁调用**：配置的真正消费者在 `flexible_plot_report.py`。以单线为例：

[genai_bench/analysis/flexible_plot_report.py:368-369](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L368-L369) 分别用 `plot_spec.x_field` 和 `y_field_spec.field` 从一个 `AggregatedMetrics` 取出 X、Y 两个数。多线版本在 [flexible_plot_report.py:511-513](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L511-L513)。这就是「声明式配置」与「真实数据」的接合点。

**静态全表 `get_available_fields`**：

[genai_bench/analysis/plot_config.py:604-647](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L604-L647) 分两部分：先遍历 `AggregatedMetrics.model_fields`（排除 `stats`）得到直连字段；再用两个写死的列表 `stats_measures`（11 个）× `stats_fields`（10 个）做双层循环，拼出所有 `stats.{field}.{measure}` 路径。它不读任何数据，只看模型定义。

**动态子表 `get_fields_from_data`**：

[genai_bench/analysis/plot_config.py:649-701](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L649-L701) 接收一个真实的 `AggregatedMetrics` 实例，遍历其字段与 `stats` 下每个 `StatField` 的每个统计量，**只收集值非 None 的**，返回 `路径 → (值, 类型名)` 的字典。CLI 的 `--list-fields` 正是用它把「这套实验数据里到底有哪些字段可用」展示给用户（见 [report.py:183-213](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L183-L213)）；当取值失败时，回退到静态全表 `get_available_fields`。

**校验助手 `validate_field_path`**：

[genai_bench/analysis/plot_config.py:703-712](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py#L703-L712) 就是「调一次 `get_field_value`，看返回是否非 None」，用于在画图前校验配置里的字段路径是否在真实数据中存在（`--validate-only`）。

#### 4.3.4 代码实践

**目标**：用静态全表法，列出所有合法字段路径，并验证 `get_field_value` 的取值逻辑。

**操作步骤**：

```python
# 示例代码
from genai_bench.analysis.plot_config import PlotConfigManager as M

fields = M.get_available_fields()
paths = sorted(fields.keys())

# 1) 直连字段（不含 '.'）
direct = [p for p in paths if "." not in p]
# 2) 统计字段（以 'stats.' 开头）
stats_paths = [p for p in paths if p.startswith("stats.")]

print("直连字段示例:", direct[:6])
print("统计字段总数:", len(stats_paths))
print("某个 ttft 路径:", [p for p in stats_paths if p.startswith("stats.ttft.")])
```

**需要观察的现象**：`stats_paths` 总数应为 \(10 \text{（指标）} \times 11 \text{（统计量）} = 110\) 条；`stats.ttft.*` 下应有 min/max/mean/stddev/sum/p25/p50/p75/p90/p95/p99 共 11 条。

**预期结果**：直连字段里能看到 `num_concurrency`、`requests_per_second`、`error_rate`、`run_duration` 等（这些正是 preset 里反复出现的 X 轴字段）。

> 待本地验证：直连字段的确切数量取决于 `AggregatedMetrics` 当前的字段总数（会随版本变化），但 `stats.*.*` 的 110 条是稳定的。

#### 4.3.5 小练习与答案

**练习 1**：对路径 `"stats.output_inference_speed.p99"`，`get_field_value` 会执行几次 `getattr`？分别是什么？

**参考答案**：3 次。依次为 `getattr(metrics, "stats")` → `getattr(..., "output_inference_speed")` → `getattr(..., "p99")`，最终拿到该指标的第 99 百分位统计值。

**练习 2**：`get_available_fields` 和 `get_fields_from_data` 都返回「可用字段」，区别是什么？`--list-fields` 优先用哪个？

**参考答案**：`get_available_fields` 基于**模型 schema**，列出理论上所有可能的字段（含值可能为 None 的），不需要真实数据；`get_fields_from_data` 基于**一个真实实例**，只列出实际有值（非 None）的字段，并附带真实数值与类型。`--list-fields` 优先用 `get_fields_from_data`（更贴合「这套实验里到底能用什么」），仅当取值异常时才回退到 `get_available_fields`。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格里的核心任务：**编写一个自定义 2×2 plot 配置 JSON，用 `PlotConfigManager.load_from_file` 加载并打印每个 `PlotSpec`**。

### 步骤 1：编写自定义 2×2 配置

在任意目录新建 `my_2x2.json`（结构参考仓库自带的 [examples/plot_configs/custom_2x2.json](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/examples/plot_configs/custom_2x2.json#L1-L45)）：

```json
{
  "layout": {"rows": 2, "cols": 2, "figsize": [16, 12]},
  "plots": [
    {
      "title": "吞吐 vs 平均延迟 (s)",
      "x_field": "mean_output_throughput_tokens_per_s",
      "y_field": "stats.e2e_latency.mean",
      "x_label": "输出吞吐 (tokens/s)",
      "y_label": "平均 E2E 延迟 (s)",
      "plot_type": "line",
      "position": [0, 0]
    },
    {
      "title": "并发 vs TTFT 多百分位",
      "x_field": "num_concurrency",
      "y_fields": [
        {"field": "stats.ttft.mean", "label": "Mean", "color": "green"},
        {"field": "stats.ttft.p99",  "label": "P99",  "color": "red", "linestyle": "--"}
      ],
      "x_label": "并发",
      "y_label": "TTFT (s)",
      "plot_type": "line",
      "position": [0, 1]
    },
    {
      "title": "RPS vs 错误率",
      "x_field": "requests_per_second",
      "y_field": "error_rate",
      "plot_type": "scatter",
      "position": [1, 0]
    },
    {
      "title": "并发 vs TPOT (s)",
      "x_field": "num_concurrency",
      "y_field": "stats.tpot.p50",
      "plot_type": "bar",
      "position": [1, 1]
    }
  ]
}
```

这张配置刻意覆盖了本讲所有要点：单线（`y_field`）、多线（`y_fields` 带样式）、三种 `plot_type`、直连字段与统计字段混用。

### 步骤 2：加载并打印

```python
# 示例代码
from genai_bench.analysis.plot_config import PlotConfigManager

cfg = PlotConfigManager.load_from_file("my_2x2.json", metrics_time_unit="ms")

print(f"网格: {cfg.layout.rows} x {cfg.layout.cols}, 画布: {cfg.layout.figsize}")
for i, p in enumerate(cfg.plots):
    kind = "多线" if p.is_multi_line() else "单线"
    ys = [y.field for y in p.get_y_field_specs()]
    print(f"[{i}] {p.title!r}  位置={p.position}  类型={p.plot_type}  {kind}  Y={ys}")
```

### 步骤 3：需要观察的现象与预期结果

- `load_from_file` 成功返回，说明 JSON 通过了 4.1 的全部校验（互斥、位置不越界不重复、`plot_type` 合法）。
- 由于传了 `metrics_time_unit="ms"`，含 `(s)` 的标题/标签被改写为 `(ms)`，例如第一张图标题变成 `吞吐 vs 平均延迟 (ms)`（中文括号不受影响，因为正则只匹配半角 `(s)`/`(seconds)`——这正好验证 4.2 讲的「只改文字标签、只认 s/ms」）。
- 打印里第 2 张图应显示为「多线」，Y 含 `stats.ttft.mean` 和 `stats.ttft.p99`；其余为「单线」。
- 把某张图的 `position` 改成与另一张相同（如两个 `[0,0]`）再加载，应抛出 `Duplicate plot position` 错误——复现 4.1 的位置去重校验。

> 待本地验证：标题单位改写只对半角 `(s)` 生效；若你用中文括号 `（s）` 则不会被替换。这一点可在步骤 3 主动验证。

### 步骤 4（选做）：把字段路径接到真实数据

如果你手头已有一个实验目录（含 run JSON，见 u6-l1），可用 CLI 一键查看真实可用字段，对照你 JSON 里写的路径是否都存在：

```bash
genai-bench plot --experiments-folder <实验目录> --group-key traffic_scenario --list-fields
```

若想只校验不画图，加 `--plot-config my_2x2.json --validate-only`（见 [report.py:270-285](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L270-L285)），它会调用 4.3 的 `validate_field_path` 逐条检查你的字段路径能否在真实 `AggregatedMetrics` 里取到值。

---

## 6. 本讲小结

- genai-bench 的绘图是**声明式**的：一份 JSON 描述「画什么」，引擎照着画。`plot_config.py` 就是这套描述语言的定义。
- 配置分四层模型：`YFieldSpec`（单条线）→ `PlotSpec`（一张子图）→ `PlotLayout`（网格）→ `PlotConfig`（顶层）。校验（`plot_type` / `y_scale` / `y_field` 与 `y_fields` 互斥 / 位置不越界不重复）焊在 Pydantic 模型上，**构造即校验**。
- `PlotConfigManager` 是配置的门面，统一三类来源：`None`（默认 preset）、preset 名、文件路径、dict；`load_config` 是分发中枢，字符串先查 `PRESETS` 再当文件。
- 共 5 个内置 preset（`2x4_default` / `2x4_tts` / `simple_2x2` / `multi_line_latency` / `single_scenario_analysis`），覆盖默认 8 图、TTS、快速 2×2、多线对比、单场景分析等典型场景。
- 加载时 `apply_time_unit_conversion` 只改写**文字标签**里的 `(s)` ↔ `(ms)`，不动字段路径；真正的数据单位换算留给绘图阶段。
- `get_field_value` 用点分路径（`stats.ttft.mean` / `num_concurrency`）做 `getattr` 下钻，是「配置字符串」与「`AggregatedMetrics` 真实数据」之间的桥梁；`get_available_fields`（静态全表）与 `get_fields_from_data`（动态子表）服务于字段发现与 `--list-fields`。

## 7. 下一步学习建议

本讲只把「配置」讲透了，但配置是如何被**消费**成真正的 PNG 图，还没有展开。建议下一讲学习 **u6-l4「灵活绘图与 plot 命令」**，重点看：

- [FlexiblePlotGenerator](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L26-L27) 如何按 `group_key`（`traffic_scenario` / `none` / 自定义）组织数据、调用本讲的 `get_field_value` 取值并喂给 matplotlib。
- 多线与多分组冲突时，系统如何自动把多线「降级」为单线（[flexible_plot_report.py:207-222](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L207-L222)）。
- `plot` 子命令如何把 `--plot-config` / `--preset` / `--validate-only` / `--list-fields` 串成完整工作流。

此外可延伸阅读 [docs/user-guide/generate-plot.md](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/user-guide/generate-plot.md) 中的「When to Use Multi-Line Plots」一节，理解多线适用的场景与坑。
