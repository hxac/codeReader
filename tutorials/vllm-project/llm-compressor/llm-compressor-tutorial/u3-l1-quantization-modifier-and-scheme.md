# QuantizationModifier 与量化方案

## 1. 本讲目标

本讲是「量化与校准管线」单元的第一篇，回答三个问题：

1. `QuantizationModifier` 这个 modifier 到底封装了什么？它的 `targets` / `ignore` / `scheme` / `config_groups` / `kv_cache_scheme` 等字段各自控制什么？
2. 一个字符串 `scheme="FP8_DYNAMIC"` 是怎样被翻译成可执行的量化方案的？它和手写 `config_groups` 是什么关系？
3. 所谓 RTN（round-to-nearest，就近取整）量化，到底在 `on_initialize` / `on_calibration_start` / `on_sequential_epoch_end` / `on_calibration_end` 这四个钩子里的**哪一个**真正完成权重取整？

学完后你应当能够：读懂 `QuantizationModifier` 的全部字段，用 `scheme` 字符串或 `config_groups` 字典两种方式描述同一个量化方案，并准确说出 RTN 权重量化的执行点。

本讲承接 [u2-l3](u2-l3-modifier-base-lifecycle.md) 讲过的 `Modifier` 基类双生命周期（校准链 `on_calibration_start`→`on_sequential_epoch_end`→`on_calibration_end`），把骨架落到一个具体的算法类上。关于 `QuantizationMixin` 如何把 scheme「挂」到 `nn.Module`、observer 与 calibration hook 的细节，本讲只讲到理解 RTN 流程所需的程度，完整精读留给 [u3-l2](u3-l2-quantization-mixin.md)。

## 2. 前置知识

在进入源码前，先用一段直觉把「量化方案」和「RTN」讲清楚。

**量化（quantization）** 的本质，是用一个低位整数去逼近一个高精度浮点数。给定原始值 \(x\)，量化用两个参数——缩放因子 \(s\)（scale）和零点偏移 \(z\)（zero-point）——把它映射到整数网格上：

\[
q = \mathrm{round}\!\left(\frac{x}{s}\right) + z,\qquad
\hat{x} = s\cdot(q - z)
\]

其中 \(\hat{x}\) 是反量化后的近似值，误差完全来自 `round` 这一步。对称量化时 \(z=0\)，更省事。 \(s\) 通常由张量的取值范围决定：

\[
s = \frac{x_{\max} - x_{\min}}{q_{\max} - q_{\min}}
\]

因此**选 scale 的策略（按通道 channel、按组 group、按张量 tensor）和取统计的方法（min/max、MSE）就是各算法的核心差异**。

- **RTN（round-to-nearest）**：最朴素的量化，scale 直接用权重自身的 min/max 算出来，对每个权重就近取整，**不需要任何校准数据**。`QuantizationModifier` 默认做的就是 RTN。
- **GPTQ / AWQ**：仍然落在同一个 `QuantizationModifier` 挂好的 scheme 上，但它们在取整之前先用校准数据额外修正权重，所以需要数据。

**量化方案（scheme）** 决定「量化成什么样」：位宽、整数还是浮点、scale 粒度、激活是否动态。命名约定里 `W` 指权重（weight）、`A` 指激活（activation），例如：

| scheme | 含义 | 典型硬件 |
|--------|------|----------|
| `FP8_DYNAMIC` | 权重 8 位浮点（静态 per-channel）、激活 8 位浮点（动态 per-token） | Hopper / Lovelace |
| `W4A16` | 权重 4 位整数、激活保持 FP16 不量化 | 任意 GPU（省显存） |
| `W8A8` | 权重与激活都是 8 位整数 | Turing+ |
| `NVFP4` | 权重与激活 4 位 NVIDIA 浮点 | Blackwell |

注意：scheme 只决定「量化成什么样」，算法（RTN/GPTQ/AWQ）决定「怎么把权重变成那样」。同一个 `W4A16` scheme，用 `QuantizationModifier` 跑就是 RTN，用 `GPTQModifier` 跑就是 GPTQ——这正是本讲要打通的关键区别。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/llmcompressor/modifiers/quantization/quantization/base.py` | `QuantizationModifier` 的全部实现，只有约 115 行，是本讲的主角 |
| `src/llmcompressor/modifiers/quantization/quantization/mixin.py` | `QuantizationMixin`，提供字段定义、scheme→config 解析、初始化/校准起止的通用逻辑。`QuantizationModifier` 继承它来复用 |
| `src/llmcompressor/modifiers/quantization/calibration.py` | `observe` / `update_qparams` 等校准函数，RTN 权重取整的实际执行者 |
| `src/llmcompressor/pipelines/data_free/pipeline.py` | 数据无关管线，说明 RTN（无校准数据）为何也能触发全部四个钩子 |
| `docs/guides/compression_schemes.md` | 各 scheme 的速查表（位宽/粒度/是否需要校准） |
| `docs/steps/choosing-scheme.md` | scheme 与 GPU 架构的对应关系 |

## 4. 核心概念与源码讲解

### 4.1 QuantizationModifier 的定位与字段体系

#### 4.1.1 概念说明

`QuantizationModifier` 是所有「权重量化 / 激活量化」动作的统一入口。它本身**不实现任何高级算法**，而是扮演两个角色：

1. 一个**量化配置容器**：描述「把哪些模块、量化成什么方案」。
2. 一个 **RTN 执行器**：当它单独出现在 recipe 里时，做的就是就近取整量化。

它的类定义只有一行，揭示了它「继承 `Modifier` 拿到生命周期 + 继承 `QuantizationMixin` 拿到量化能力」的组合：

```python
class QuantizationModifier(Modifier, QuantizationMixin):
```

`Modifier`（[u2-l3](u2-l3-modifier-base-lifecycle.md) 讲过的模板方法基类）提供 `on_initialize` 等钩子；`QuantizationMixin` 提供量化字段和 `initialize_quantization` / `start_calibration` / `end_calibration` 三个工具方法。`QuantizationModifier` 自己只负责把这两者按正确顺序串起来。

#### 4.1.2 核心流程

量化字段分两组，需要先分清「输入」和「派生」：

```
用户输入字段（你写在 recipe 里的）
├── targets        目标模块名（默认 ["Linear"]）
├── ignore         要排除的模块名（默认 []）
├── scheme         预设方案名或字典，如 "FP8_DYNAMIC" 或 {"W8A8": ["Linear"]}
├── config_groups  手写的完整方案字典（与 scheme 二选一）
└── kv_cache_scheme 对 KV cache 的量化（可选）

派生字段（代码内部算出来的，不要自己填）
├── resolved_config   由 scheme 或 config_groups 解析出的 QuantizationConfig
└── resolved_targets  最终真正命中的目标模块集合
```

关键规则：`scheme` 和 `config_groups` **只能填一个**。`scheme` 是「快捷方式」，`config_groups` 是「完整手写」，二者最终都汇成一个 `QuantizationConfig`。

#### 4.1.3 源码精读

字段的真正定义在 `QuantizationMixin` 里，而不是 `QuantizationModifier` 自己。这是为了让 GPTQ 等子类也能复用同一套字段：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:126-139](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L126-L139) — 定义 `config_groups`/`targets`/`ignore`/`scheme`/`kv_cache_scheme` 五个核心字段，以及一组 observer 覆盖字段。`targets` 默认是 `["Linear"]`，意味着「不指定就只量化 Linear 层」。

注意一个常被忽略的设计：`targets` 字段**不是目标的唯一来源**。因为目标也可以写在 `config_groups` 内部，所以代码里专门提供了一个 `resolved_targets` 属性作为「真正的命中目标集合」，并反复提示不要直接读 `targets`：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:202-219](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L202-L219) — `resolved_targets` 遍历 `resolved_config` 里所有 `config_group` 的 `targets` 汇总，若设了 `kv_cache_scheme` 还会补上 `KV_CACHE_TARGETS`。

`QuantizationModifier` 自己的类体里，字段只是通过 docstring 文档化（见下），真正存校验逻辑的是 mixin。它的类 docstring 把五个字段逐一解释，是最权威的字段说明：

[src/llmcompressor/modifiers/quantization/quantization/base.py:24-56](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L24-L56) — 类声明与字段文档。重点看 `scheme` 一项：它既可以是预设名（如 `"FP8_DYNAMIC"`），也可以是 `{"预设名": [targets]}` 这种字典。

#### 4.1.4 代码实践

1. **实践目标**：分清「输入字段」和「派生字段」。
2. **操作步骤**：阅读 mixin.py 的 `has_config()` 方法（[mixin.py:321-331](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L321-L331)），它用四个输入字段的「全默认值」来判断用户是否真的配置了量化。
3. **需要观察的现象**：当你只写 `QuantizationModifier()`（全部默认）时，`has_config()` 返回 `False`，`on_initialize` 会直接报错。
4. **预期结果**：理解「空配置」与「有效配置」的边界——必须至少改动 `scheme` / `config_groups` / `targets` / `ignore` / `kv_cache_scheme` 之一，才算一个合法的量化 modifier。

#### 4.1.5 小练习与答案

**练习 1**：如果我写 `QuantizationModifier(targets="Linear")`，`has_config()` 返回什么？这个 modifier 合法吗？

> **答案**：返回 `False`。因为 `targets` 被设成了默认值 `["Linear"]`，等同于没改任何字段，`has_config()` 视为「没配置」。这个 modifier 跑到 `on_initialize` 时会抛出 `QuantizationModifier requires that quantization fields be specified`。要让它合法，至少要指定 `scheme` 或 `config_groups`。

**练习 2**：`resolved_targets` 相比 `targets` 字段多了哪些来源？

> **答案**：除了 `targets`（经 config_groups 透传）之外，`resolved_targets` 还会并入每个 `config_group` 内部各自声明的 `targets`，以及（若设置了 `kv_cache_scheme`）`KV_CACHE_TARGETS`（即 `q_proj`/`k_proj` 等）。

---

### 4.2 scheme 预设与 config_groups 的等价关系

#### 4.2.1 概念说明

`scheme="FP8_DYNAMIC"` 这种字符串写法很方便，但底层需要一个「翻译」过程，把字符串展开成完整的 `QuantizationConfig`。这一步由 `resolve_quantization_config()` 完成。理解它，你就能在「快捷方式（scheme）」和「完整手写（config_groups）」之间自由切换，也能看懂保存后 `config.json` 里 `quantization_config` 的结构。

预设方案名（preset scheme）来自上游 `compressed_tensors` 库，常见的有 `FP8_DYNAMIC`、`FP8_BLOCK`、`W8A8`、`W4A16`、`W4A16_ASYM`、`NVFP4`、`MXFP4` 等，这些名字在仓库的示例和测试里都能看到（例如 `docs/guides/entrypoints/oneshot.md:154` 用了 `"W4A16"`）。

#### 4.2.2 核心流程

`resolve_quantization_config` 的决策树：

```
resolve_quantization_config():
  if scheme 与 config_groups 同时给出 → 报错（二选一）
  if scheme 给出:
      若 scheme 是字符串预设名 → 展开成 {预设名: targets}
      遍历 scheme 字典的每个键:
          若键是预设名   → preset_name_to_scheme(键, targets)  # 查表展开
          否则           → 当成手写的 QuantizationScheme
          套用 observer 覆盖
          存入 config_groups["group_0"], ["group_1"] ...
  elif config_groups 给出 → 直接用，套用 observer 覆盖
  else（都没给）          → 用默认 QuantizationScheme(targets=targets) 作 group_0
  组装并返回 QuantizationConfig
```

换句话说，`scheme` 永远会被归一化成 `config_groups`，二者殊途同归。

#### 4.2.3 源码精读

互斥校验在函数最前面：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:333-384](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L333-L384) — `resolve_quantization_config` 全貌。第 343-344 行强制 `scheme` 与 `config_groups` 二选一。

字符串预设展开成字典、再查表实例化的核心几行：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:346-366](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L346-L366) — 字符串 `scheme` 先被包成 `{scheme: targets}`，再对每个预设名调用 `preset_name_to_scheme(key, targets)` 查表得到完整 `QuantizationScheme`，最后放进 `group_0`/`group_1`。

`validate_scheme` 在赋值时就把非法的预设名挡掉，避免等到运行才报错：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:151-168](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L151-L168) — `scheme` 字段校验器：字符串必须是预设名，否则抛 `must either be a preset scheme name ...`；字典则递归校验每个键。

至于「哪些预设名合法」，速查表在文档里：

[docs/steps/choosing-scheme.md:11-20](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md#L11-L20) — scheme 与精度、目标、最低算力的对应表。例如 `W4A16/W8A16` 最低算力 7.5（Turing），`W8A8-FP8` 最低 8.9（Lovelace），`NVFP4`/`MXFP4` 最低 10.0（Blackwell）。

[docs/guides/compression_schemes.md:17-24](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/compression_schemes.md#L17-L24) — `FP8_DYNAMIC` 的细节：权重 per-channel、激活动态 per-token，**用 RTN 时无需校准数据**，激活量化发生在 vLLM 推理时。

#### 4.2.4 代码实践

1. **实践目标**：验证「scheme 字符串」与「config_groups 手写」产出等价的配置。
2. **操作步骤**：在 Python 里构造两个 modifier，比较它们的 `resolved_config`：
   ```python
   # 示例代码（非项目原有）
   from llmcompressor.modifiers.quantization import QuantizationModifier
   m1 = QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=["lm_head"])
   print(m1.resolved_config)          # 由 scheme 展开而来
   ```
3. **需要观察的现象**：`resolved_config.config_groups["group_0"]` 里权重是 8 位 float、per-channel，激活是 8 位 float、dynamic=True。
4. **预期结果**：理解 `scheme` 只是 `config_groups` 的语法糖。**待本地验证**：你环境里的具体字段名以实际打印为准。

#### 4.2.5 小练习与答案

**练习 1**：`scheme="W4A16"` 会被展开成什么样的权重量化参数？

> **答案**：权重 4 位整数（type=int）、按组量化（strategy=group，group_size 通常为 128）、激活不量化（保持 FP16）。保存后对应的 compressor 是 `pack_quantized`（见 `docs/steps/choosing-scheme.md` 的 Compression Formats 表）。

**练习 2**：如果我同时传 `scheme` 和 `config_groups`，会怎样？

> **答案**：`resolve_quantization_config` 第 343-344 行直接抛 `Please specify either scheme or config_groups`。两者是互斥的两种描述方式。

---

### 4.3 RTN 量化在四个生命周期钩子中的执行点

这是本讲最核心的一节：把 RTN 量化的「取整动作」精确定位到代码行。

#### 4.3.1 概念说明

回忆 [u2-l3](u2-l3-modifier-base-lifecycle.md)：`Modifier` 基类把校准生命周期拆成一条链 `on_calibration_start`→`on_sequential_epoch_end`→`on_calibration_end`，加上初始化时的 `on_initialize`，正好四个钩子。`QuantizationModifier` 把这四个钩子一一实现，把 RTN 量化切成四个阶段。

一个关键直觉：**「挂方案」和「算 scale」是两回事**。
- `on_initialize` 只是把 scheme「挂」到模块上（模块从此知道「我要被量化成 W4A16」），但此时量化是关闭的，权重还没动。
- 真正读权重、算 scale/zero-point、把权重取整，发生在 `on_sequential_epoch_end` 里，靠 `observe`（收集统计）+ `update_qparams`（写回 scale/zp）两步完成。

#### 4.3.2 核心流程

RTN 的四阶段时序：

```
on_initialize            （挂方案）
  └─ initialize_quantization(model)
       ├─ 给命中模块挂 QuantizationScheme
       ├─ 校验 group_size 整除性
       └─ 关闭量化（disable_quantization）  ← 此时前向仍是全精度

on_calibration_start     （打开校准）
  └─ start_calibration(model)
       ├─ 给模块挂 observer + calibration hook
       └─ 打开量化（apply_calibration_status）

on_sequential_epoch_end  （★ 真正取整 ★）
  ├─ 过滤出已量化的模块
  ├─ update_qparams(modules, ACTIVATION_OBS)   # 激活的 scale
  └─ observe(modules, "weight")                 # 收集权重 min/max
     update_qparams(modules, "weight")          # 算 scale/zp 并写回 ← RTN 在这里

on_calibration_end       （收尾）
  └─ end_calibration(model)
       ├─ 移除 calibration hook 与 observer
       └─ 冻结（freeze），保持量化开启供后续推理
```

注意：即便 RTN **不需要校准数据**，这四个钩子仍然会全部触发——因为数据无关管线 `DataFreePipeline` 会显式调用三个校准回调：

[src/llmcompressor/pipelines/data_free/pipeline.py:37-39](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py#L37-L39) — 数据无关管线没有前向传播，但仍然依次触发 `calibration_start()`、`sequential_epoch_end(全部模块)`、`calibration_end()`。区别只在于：它把**整个模型的所有模块**一次性传给 `sequential_epoch_end`，且中间不跑前向、不传播量化误差。

这正是 RTN 能在「无数据」下完成取整的原因——权重取整只依赖权重自身的 min/max，不需要任何激活输入。

#### 4.3.3 源码精读

四个钩子的实现极简，每个都只是对 mixin 工具方法的一行转发（除了 `on_sequential_epoch_end` 自己干了权重量化的活）：

[src/llmcompressor/modifiers/quantization/quantization/base.py:58-75](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L58-L75) — `on_initialize`：先 `has_config` 防御（没配字段就报错），再调 `initialize_quantization` 挂方案。这是「挂方案、关量化」阶段。

[src/llmcompressor/modifiers/quantization/quantization/base.py:77-81](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L77-L81) — `on_calibration_start`：一行转发 `start_calibration`，挂 observer 与校准 hook、打开量化。

[src/llmcompressor/modifiers/quantization/quantization/base.py:83-108](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L83-L108) — `on_sequential_epoch_end`：**RTN 权重取整的真正执行点**。第 86 行先过滤出真正量化的模块；第 87-88 行处理激活统计与 scale；第 91-94 行是非分布式分支——`observe(modules, "weight")` 收集权重统计、`update_qparams(modules, "weight")` 算出 scale/zero_point 写回模块。这就是「就近取整」发生的地方。

[src/llmcompressor/modifiers/quantization/quantization/base.py:110-114](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L110-L114) — `on_calibration_end`：转发 `end_calibration`，卸 hook、删 observer、冻结。

`observe` 与 `update_qparams` 这两步到底做了什么，在 calibration.py 里：

[src/llmcompressor/modifiers/quantization/calibration.py:86-106](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L86-L106) — `observe`：取出模块上的 `{base_name}_observer`，把模块的对应张量（如 `weight`）喂给它累积统计。对 RTN 来说，observer 默认是 memoryless min/max，即只记当前权重的 min/max。

[src/llmcompressor/modifiers/quantization/calibration.py:109-162](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L109-L162) — `update_qparams`：让 observer 用累积的统计算出 scale/zero_point（`observer.get_qparams()`），写回模块的 `weight_scale`/`weight_zero_point` 等参数。动态激活（dynamic=True）会跳过 scale/zp 的写入（留给推理时算）。

还有一个对 RTN 很关键的细节：权重的 observer 会被强制降级成 memoryless 版本以省内存：

[src/llmcompressor/modifiers/quantization/calibration.py:52-78](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L52-L78) — `initialize_observer` 在 base_name 为 `"weight"` 时，把 `static_minmax`/`minmax` 改写成 `memoryless_minmax`、把 `mse` 改写成 `memoryless_mse`。注释里点明原因：「training is no longer supported: always use memoryless for weights」。

#### 4.3.4 代码实践

1. **实践目标**：用断点/日志确认 RTN 权重取整发生在 `on_sequential_epoch_end`。
2. **操作步骤**：阅读 `on_sequential_epoch_end`（[base.py:83-108](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L83-L108)），在 `update_qparams(modules, "weight")`（第 93 行）前后各加一行 `print`（**示例代码**，仅为观察用，实践后请还原）：
   ```python
   print("before update:", modules[0].weight_scale if hasattr(modules[0],'weight_scale') else None)
   update_qparams(modules, "weight")
   print("after  update:", modules[0].weight_scale)
   ```
3. **需要观察的现象**：取整前 `weight_scale` 为空（或未赋值），取整后变为按通道/group 计算出的 scale 张量。
4. **预期结果**：亲眼看到「权重取整」这一步只发生在 `on_sequential_epoch_end`，前三个钩子都不改权重数值。**待本地验证**：具体属性名与张量形状依 scheme 而定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 RTN 不需要校准数据，却仍然要走完 `on_calibration_start`/`on_sequential_epoch_end`/`on_calibration_end` 三个钩子？

> **答案**：因为 RTN 的「权重取整」只依赖权重自身的 min/max，但取整动作本身（observe+update_qparams、挂/卸 hook、冻结）仍然要由这三个钩子驱动。`DataFreePipeline`（[data_free/pipeline.py:37-39](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py#L37-L39)）就是为此而存在：它不跑前向，但照样触发这三个回调，把所有模块一次性交给 `on_sequential_epoch_end`。

**练习 2**：`on_initialize` 里 `disable_quantization`（关闭量化）的目的是什么？

> **答案**：挂上 scheme 后，模块前向会「模拟量化执行」。但在校准真正开始前，我们不想让量化生效（否则会影响后续统计或干扰上游模块）。所以先 disable，等 `on_calibration_start` 的 `apply_calibration_status` 再打开。这保证了「挂方案」与「启用量化」在时间上分离。

**练习 3**：GPTQ 和 RTN 都继承自带量化的能力，它们在「取整」这一步的差别体现在哪里？

> **答案**：两者都复用 `on_sequential_epoch_end`。RTN（`QuantizationModifier`）在这一步只做 `observe(weight)`+`update_qparams(weight)`，用 min/max 直接取整；GPTQ（`GPTQModifier`，见 [u4-l1](u4-l1-gptq-algorithm.md)）在校准阶段额外累积 Hessian，在 epoch end 时用 Hessian 修正后再取整，因此需要校准数据。

---

### 4.4 scheme 如何决定是否需要校准数据

#### 4.4.1 概念说明

`QuantizationModifier` 有一个从 `Modifier` 继承来的属性 `requires_calibration_data`。它不是用户手填的，而是**根据 scheme 自动推断**的。这个推断直接决定了 oneshot 会选用哪条校准管线（sequential 还是 datafree），也决定了你是否需要准备数据集。

直觉判断：
- 只要 scheme 里有「需要静态校准的激活」（input/output 不是 dynamic）、或权重 observer 用了 `imatrix_mse`、或开了 `kv_cache_scheme`，就需要数据。
- 反之，像 `FP8_DYNAMIC`（激活动态）、`W4A16`（只量化权重、激活保持 FP16）这种，RTN 下都不需要数据。

#### 4.4.2 核心流程

推断逻辑（伪代码）：

```
_set_requires_calibration_data():   # model_validator(mode="after")
  if 已经被设成 True        → 直接返回
  if kv_cache_scheme != None → requires_calibration_data = True
  for 每个 config_group:
      if 权重 observer == "imatrix_mse"        → True
      if input_activations 静态或 LOCAL        → True
      if output_activations 静态（非 dynamic） → True
  否则保持默认（False，无需数据）
```

#### 4.4.3 源码精读

推断逻辑用一个 `model_validator(mode="after")` 实现，在 modifier 实例化之后自动跑：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:296-319](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L296-L319) — `_set_requires_calibration_data`：先看 `kv_cache_scheme`，再遍历每个 group 检查权重 observer 是否为 `imatrix_mse`、input/output 激活是否为非动态。只要命中一条就把 `requires_calibration_data` 置 True。

把这个推断和管线选择串起来（详见 [u3-l4](u3-l4-calibration-pipeline-registry.md)）：`CalibrationPipeline.from_modifiers` 会依据各 modifier 的 `requires_calibration_data` 推断管线——需要数据用 sequential、不需要用 datafree。所以 `QuantizationModifier(scheme="FP8_DYNAMIC")` 推断出 datafree，无需 dataset；而换成静态激活的 scheme 就会被推断成 sequential，必须传 dataset。

文档侧也反复强调「FP8 动态 / W4A16 用 RTN 时无需校准数据」：

[docs/guides/compression_schemes.md:46-54](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/compression_schemes.md#L46-L54) — W4A16/W8A16 一节，明确「Optimally compressed using non-RTN algorithms (GPTQ, AWQ) which require a dataset」，反过来即 RTN 不需要数据。

#### 4.4.4 代码实践

1. **实践目标**：用代码验证不同 scheme 对 `requires_calibration_data` 的推断。
2. **操作步骤**（**示例代码**）：
   ```python
   from llmcompressor.modifiers.quantization import QuantizationModifier
   for s in ["FP8_DYNAMIC", "W4A16", "W8A8"]:
       m = QuantizationModifier(targets="Linear", scheme=s, ignore=["lm_head"])
       print(s, "→ requires_calibration_data =", m.requires_calibration_data)
   ```
3. **需要观察的现象**：`FP8_DYNAMIC` 与 `W4A16` 应推断为 `False`（RTN 无需数据），而带静态激活的方案可能为 `True`。
4. **预期结果**：理解 scheme → `requires_calibration_data` → 管线选择 这条因果链。**待本地验证**：`W8A8` 等方案的布尔值以你环境实际为准（取决于其预设的激活是否动态）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `kv_cache_scheme` 一旦设置就强制需要校准数据？

> **答案**：KV cache 量化要对 `q_proj`/`k_proj` 的**输出**做静态量化（把 key/value 压缩后再存进 cache），输出激活是静态的、必须用数据校准出 scale。代码里因此把它列为「需要数据」的第一条触发条件（[mixin.py:301-303](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L301-L303)）。

**练习 2**：`FP8_DYNAMIC` 为什么不需要数据？

> **答案**：它的权重是静态 per-channel（scale 由权重 min/max 算，RTN 即可），激活是**动态** per-token（scale 在 vLLM 推理时实时算，不落盘）。代码里 input/output 激活的检查都针对「非动态」，动态激活不会触发 `requires_calibration_data=True`。

---

## 5. 综合实践：对比 FP8_DYNAMIC 与 W4A16 的量化产物

本任务把本讲四个模块串起来：用 `QuantizationModifier` 分别以 `FP8_DYNAMIC` 和 `W4A16` 两个 scheme 跑 RTN 量化，比较保存后 `config.json` 里 `quantization_config` 的差异。

### 5.1 实践目标

亲手验证：
1. 两个 scheme 都推断为「无需校准数据」（走 datafree 管线）。
2. RTN 权重取整都发生在 `on_sequential_epoch_end`。
3. 二者产出的 `quantization_config` 在 **format、位宽、类型、scale 粒度、是否有激活量化** 上明显不同。

### 5.2 操作步骤

准备一个脚本（**示例代码**，基于 [docs/guides/entrypoints/oneshot.md:124-139](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/entrypoints/oneshot.md#L124-L139) 的 FP8 范例改写）：

```python
# 示例代码：compare_schemes.py
from transformers import AutoModelForCausalLM
from llmcompressor import oneshot
from llmcompressor.modifiers.quantization import QuantizationModifier

MODEL_ID = "facebook/opt-125m"   # 任选一个本地能加载的小模型

for scheme, out in [("FP8_DYNAMIC", "opt-125m-FP8"), ("W4A16", "opt-125m-W4A16")]:
    model = AutoModelForCausalLM.from_pretrained(MODEL_ID, torch_dtype="auto")
    recipe = QuantizationModifier(targets="Linear", scheme=scheme, ignore=["lm_head"])
    oneshot(model=model, recipe=recipe, output_dir=out)
```

运行后，分别打开 `opt-125m-FP8/config.json` 与 `opt-125m-W4A16/config.json`，定位 `quantization_config` 字段（这是 compressed-tensors 格式量化生效的证据，详见 [u6-l3](u6-l3-saving-and-compressed-tensors.md)）。

### 5.3 需要观察的现象与预期结果

对照下表比较两个 `quantization_config`（预期差异，基于 [docs/steps/choosing-scheme.md:77-91](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md#L77-L91) 的 Compression Formats 表）：

| 维度 | FP8_DYNAMIC | W4A16 |
|------|-------------|-------|
| `format`（compressor） | `float_quantized` | `pack_quantized` |
| 权重 `num_bits` | 8 | 4 |
| 权重 `type` | `float` | `int` |
| 权重 `strategy` | `channel`（per-channel） | `group`（group_size=128） |
| `input_activations` | 8 位 float，`dynamic=true` | 无（激活不量化，保持 FP16） |
| 是否需要校准数据 | 否（动态激活） | 否（只量化权重） |

**待本地验证**：上表字段名/取值以你实际产出的 `config.json` 为准；不同版本的 `compressed_tensors` 预设细节可能略有差异。运行本身一般不要求 FP8 硬件——oneshot 产出的是「模拟量化」的 checkpoint，FP8 真正的算子加速只发生在 vLLM 推理时。

### 5.4 进阶观察

- 在 oneshot 日志里确认两次运行都走了 **datafree**（或经由 independent 委派的 datafree）管线，且都没有下载/迭代校准数据集——印证 4.4 的推断。
- 用 `du -sh` 对比两个输出目录的权重体积：W4A16 应明显小于 FP8（4 位 vs 8 位）。

## 6. 本讲小结

- `QuantizationModifier(Modifier, QuantizationMixin)` 是量化动作的统一入口：自身只做 RTN，字段定义与 scheme 解析都复用自 `QuantizationMixin`。
- 五个核心字段：`targets`（默认 Linear）、`ignore`、`scheme`（预设快捷方式）、`config_groups`（完整手写，与 scheme 二选一）、`kv_cache_scheme`。判断目标集合要用 `resolved_targets` 而非 `targets`。
- `scheme` 字符串通过 `resolve_quantization_config` 展开成 `config_groups`，二者等价；非法预设名在 `validate_scheme` 阶段就被拦下。
- RTN 量化的「权重取整」精确发生在 `on_sequential_epoch_end` 的 `observe(weight)`+`update_qparams(weight)`；前两个钩子只负责挂方案与开关量化，最后一个钩子负责卸 hook 与冻结。
- 即使无需数据，`DataFreePipeline` 也会触发全部校准回调，所以 RTN（如 `FP8_DYNAMIC`/`W4A16`）能在不带 dataset 的情况下完成取整。
- `requires_calibration_data` 由 scheme 自动推断（静态激活 / `imatrix_mse` / `kv_cache_scheme` 触发），并进一步决定 oneshot 选 sequential 还是 datafree 管线。

## 7. 下一步学习建议

- 阅读 [u3-l2](u3-l2-quantization-mixin.md) 深入 `QuantizationMixin`：`initialize_quantization` 如何把 scheme 挂到具体 `nn.Module`、`start_calibration`/`end_calibration` 如何管理 observer 与 calibration hook。
- 阅读 [u3-l3](u3-l3-calibration-observers-hooks.md) 了解 `observe`/`update_qparams` 背后的 observer（MinMax/MSE/IMatrix）如何收集统计并融合。
- 想看「需要数据」的对照，直接进 [u4-l1](u4-l1-gptq-algorithm.md)（GPTQ），观察它在同一个 `on_sequential_epoch_end` 钩子里如何用 Hessian 替代纯 min/max 取整。
- 想理解管线如何被选择，阅读 [u3-l4](u3-l4-calibration-pipeline-registry.md) 的 `CalibrationPipeline.from_modifiers` 与 `_infer_pipeline`。
