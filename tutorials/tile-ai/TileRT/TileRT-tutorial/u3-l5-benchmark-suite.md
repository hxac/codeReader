# 性能基准测试套件：BenchMode、workload 与汇总表

## 1. 本讲目标

本讲讲解 TileRT 自带的性能基准测试套件 `tilert/benchmark`。学完后你应当能够：

- 说清一次基准测试是「若干个 `BenchMode` × 若干个 workload」组成的网格，以及默认跑的是哪 3 个模式、哪 3 类 workload。
- 解释 `apply_mode` 是如何把一个 `BenchMode` 翻译成 `generator.update_sampling_params`、并在必要时触发 CUDA Graph 重捕获的（承接 [u3-l4](u3-l4-sampling-and-cuda-graph.md)）。
- 读懂汇总表里 `tok/s`、`it/s`、`acc` 三行的统计来源，尤其是 MTP 模式与非 MTP 模式在计算吞吐时的根本差异。
- 看懂 `generate.py` 里 `--modes` 与 `--workloads` 两个过滤参数的匹配规则，并能据此裁剪出一次最小基准。

## 2. 前置知识

本讲默认你已掌握以下内容（来自前置讲义）：

- **CLI 入口与 `__main__` 链路**（[u1-l4](u1-l4-cli-entry-and-generation-flow.md)）：知道 `python -m tilert.generate` 在非交互模式下会进入基准测试分支，且基准模式下 `with_mtp` 被强制为 `True`。
- **Generator 生命周期与 `generate` 返回值**（[u1-l5](u1-l5-generator-api-and-lifecycle.md)、[u3-l2](u3-l2-generation-loop-without-mtp.md)、[u3-l3](u3-l3-mtp-speculative-decoding.md)）：`generate` 返回 `(text, time_list, accepted_counts, prompt_len)`；非 MTP 时 `accepted_counts` 为空，MTP 时 `accepted_counts` 是每步接受的 token 数。
- **采样与 CUDA Graph 重捕获**（[u3-l4](u3-l4-sampling-and-cuda-graph.md)）：知道采样四元组 `(temperature, top_p, top_k, use_topp)` 被固化进 CUDA Graph，改变它需要 `go_home` 释放旧图、`prepare_money` 重捕新图。

补充几个本讲要用到的基础概念：

- **TPOT / 吞吐**：TileRT 的优化标尺是「单 token 延迟」（TPOT），基准测试既报告「每秒产出多少 token」（tok/s，偏向吞吐），也报告「每秒推进多少个解码步」（it/s，即 iteration/s，偏向原始延迟）。两者在 MTP 下会显著不同。
- **稳态吞吐（steady-state）**：模型在预热（warmup）之后、各缓存与 CUDA Graph 都已就绪时的吞吐。`short_prompt` 基准用「1 次预热 + 20 次重复」来专门测量它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilert/benchmark/\_\_init\_\_.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py) | 基准套件的核心：定义 `BenchMode`/`CellStats`/`PerStepData` 等数据结构，提供 `apply_mode`、`merge_stats`、`print_summary_table` 三个工具函数。 |
| [tilert/benchmark/short_prompt.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/short_prompt.py) | 短 prompt workload：1 次预热 + 20 次迭代，测稳态解码吞吐，列名 `Short@200`。 |
| [tilert/benchmark/coding_prompt.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/coding_prompt.py) | 编码 prompt workload：单次生成，列名 `Coding`。 |
| [tilert/benchmark/long_prompt.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/long_prompt.py) | 长 prompt workload：单次长生成，并按 2048/512 token 切分报告前后段 it/s，列名 `Long`。 |
| [tilert/benchmark/config.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/config.py) | 配置加载：`get_weights_dir` 从 `~/.tilert/config.toml` 解析权重目录（详见 [u1-l4](u1-l4-cli-entry-and-generation-flow.md)）。 |
| [tilert/generate.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py) | CLI 入口：构造 3 个默认 `BenchMode`、按 `--modes`/`--workloads` 过滤、跑 workload 网格、合并并打印汇总表。 |

一句话概括分工：`__init__.py` 提供「数据结构 + 工具函数」，三个 `*_prompt.py` 文件各自定义「一类 prompt 的跑法」，`generate.py` 负责「把模式与 workload 组成网格并打印结果」。

---

## 4. 核心概念与源码讲解

### 4.1 BenchMode 与 workload 定义

#### 4.1.1 概念说明

基准测试要回答的问题是：**在一组固定的采样设置下，这个模型的吞吐与延迟如何？** 为了让结果可比、可复现，TileRT 把两个正交的维度各自抽象成一个数据结构：

- **`BenchMode`（模式）**：一份**固定的采样配置**。它把 `(with_mtp, temperature, top_p, top_k, use_topp)` 打包在一起，再加一个用于显示的 `label`。一个模式就是表格里的一「行块」。
- **workload（工作负载）**：一份**固定的 prompt 与迭代策略**（跑几次、在哪几个 token 检查点上统计）。一个 workload 就是表格里的一「列」。

于是基准测试天然是一个**网格（grid）**：`模式数 × workload 数` 个组合，每个组合产生一个单元格（`CellStats`）。

> 注意：这里的「模式」与采样里的 `use_topp` 模式不是一回事。`BenchMode` 是**基准测试层面的预设档位**，`use_topp` 是**采样算法层面的开关**（top-1/top-k 走一条路，top-p 走另一条路）。一个 `BenchMode` 内部会设置 `use_topp`。

#### 4.1.2 核心流程

`generate.py` 在非交互分支里硬编码了 **3 个默认模式**，构成「逐级开启投机解码 / 切采样算法」的对照：

```
模式 1: top-k1  w/o MTP   —— 不开 MTP，看裸解码吞吐
模式 2: top-k1  w/   MTP   —— 开 MTP，看投机解码加速
模式 3: top-p0.95 w/  MTP  —— 开 MTP，同时切到 top-p 采样
```

随后对每个 workload 的 `run(generator, modes)` 串行跑一遍这 3 个模式。整体流程：

```text
__main__
  ├── 构造 3 个 BenchMode（generate.py:236-248）
  ├── --modes 过滤：按 label 子串筛选（generate.py:250-257）
  ├── 选 workload：short / coding / long（generate.py:259-276）
  ├── 对每个 workload 调 run(generator, modes)  ──┐
  │        （workload 内部对每个 mode 调 apply_mode → generate）│
  ├── 收集每个 workload 的 BenchStats              │  网格
  ├── merge_stats(...) 按模式 label 合并各列        │
  └── print_summary_table(...) 打印 3 行/模式的表  ──┘
```

#### 4.1.3 源码精读

`BenchMode` 是一个带默认值的 dataclass，定义采样所需的全部字段：

[tilert/benchmark/\_\_init\_\_.py:11-20](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L11-L20) —— `BenchMode`：`with_mtp` 与 `label` 是必填，`use_topp/top_p/top_k/temperature` 给了默认值（`False / 1.0 / 256 / 1.0`）。

3 个默认模式在 `generate.py` 里就地构造：

[tilert/generate.py:236-248](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L236-L248) —— 构造 3 个 `BenchMode`。要点：

- 前两个模式（`top-k1 w/o MTP`、`top-k1 w/ MTP`）**不显式传采样参数**，因而走 `BenchMode` 的默认值：`top_k=256, use_topp=False, top_p=1.0, temperature=1.0`，二者**只在 `with_mtp` 上不同**，构成「开/关 MTP」的纯净对照。这里的 `top-k1` 是显示用的档位名，对应的实际采样是 dataclass 默认值（`top_k=256`）。
- 第三个模式（`top-p{bench_top_p} w/ MTP`）显式设 `use_topp=True`、`top_p=bench_top_p`，并带 `with_mtp=True`，即在 MTP 之上再切到 top-p 采样。
- `bench_top_p` 的取值见 [tilert/generate.py:236](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L236)：`args.top_p if args.top_p < 1.0 else 0.95`。也就是说 CLI 默认 `--top-p 1.0` 时，第三个模式用的是 `0.95`，label 变成 `top-p0.95 w/ MTP`——这正是 `--modes` 帮助文本里写 `top-p0.95` 的原因。

三类 workload 各自定义自己的 `PROMPT` 与迭代策略：

[tilert/benchmark/short_prompt.py:17-19](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/short_prompt.py#L17-L19) —— 短 prompt：`PROMPT = "Tell me 10 jokes..."`，`NUM_ITERS = 20`，`TOKEN_CHECKPOINTS = [200]`（在第 200 个 token 处统计），列名 `Short@200`。

[tilert/benchmark/coding_prompt.py:17](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/coding_prompt.py#L17) —— 编码 prompt：`"Hi, can you write a sort program in C for me?"`，单次生成，列名 `Coding`。

[tilert/benchmark/long_prompt.py:17](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/long_prompt.py#L17) —— 长 prompt：要求约 3000 字的长故事，单次长生成，列名 `Long`。

> 三个 workload 的共同契约：都有一个 `run(generator, modes) -> tuple[BenchStats, PerStepDict]` 函数，内部对每个 mode 调 `apply_mode` 后用 `generator.generate(...)` 跑数据。`BenchStats` 是「模式 label → {列名 → CellStats}」的嵌套字典（见 [tilert/benchmark/\_\_init\_\_.py:32](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L32)）。

#### 4.1.4 代码实践

**目标**：在不跑模型的前提下，验证 3 个默认模式的真实采样参数。

**步骤**：

1. 阅读 [tilert/generate.py:236-248](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L236-L248) 与 [tilert/benchmark/\_\_init\_\_.py:11-20](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L11-L20)。
2. 在纸上（或如下「示例代码」）填出 3 个模式的实际参数：

```python
# 示例代码：手工复现 generate.py 的 3 个默认模式（假设 args.top_p=1.0, args.top_k=256, args.temperature=1.0）
from tilert.benchmark import BenchMode

bench_top_p = 0.95  # 因为 args.top_p == 1.0，走 else 分支
modes = [
    BenchMode(with_mtp=False, label="top-k1 w/o MTP"),
    BenchMode(with_mtp=True,  label="top-k1 w/ MTP"),
    BenchMode(with_mtp=True, label=f"top-p{bench_top_p} w/ MTP",
              use_topp=True, top_p=bench_top_p, top_k=256, temperature=1.0),
]
for m in modes:
    print(m.label, "→", dict(with_mtp=m.with_mtp, top_p=m.top_p, top_k=m.top_k,
                              use_topp=m.use_topp, temperature=m.temperature))
```

**需要观察的现象**：前两个模式的 `top_k/top_p/use_topp/temperature` 完全相同，唯一区别是 `with_mtp`。

**预期结果**：第三个模式的 `use_topp=True` 且 `top_p=0.95`；前两个模式的 `use_topp=False` 且 `top_p=1.0`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 CLI 改成 `--top-p 0.8`，第三个模式的 label 会变成什么？前两个模式会受影响吗？

> **答案**：label 变成 `top-p0.8 w/ MTP`（因为 `args.top_p < 1.0` 成立，`bench_top_p = 0.8`）。前两个模式不受影响——它们不读取 `args.top_p`，永远走 `BenchMode` 默认值。

**练习 2**：为什么基准测试需要「开 MTP」与「不开 MTP」两个模式做对照，而不是只测一个？

> **答案**：MTP 的收益体现在「一次 forward 平均产出多少个 token」（即接受长度 μ，见 [u3-l3](u3-l3-mtp-speculative-decoding.md)）。只有与「不开 MTP 的裸解码」放在同一张表里对比，才能直观看到 tok/s 的提升与 acc_rate（平均接受长度）的对应关系。

---

### 4.2 apply_mode 运行时切换

#### 4.2.1 概念说明

`apply_mode` 是 `BenchMode`（数据）与 `generator`（执行器）之间的**唯一桥梁**：它把一个模式里存的采样参数，写进生成器当前生效的采样配置。它本身只有 5 行，但背后串起了 [u3-l4](u3-l4-sampling-and-cuda-graph.md) 讲过的整条「采样配置 → CUDA Graph 重捕获」链路。

关键认知（承接 u3-l4）：**采样四元组被固化进了 CUDA Graph**。所以切换模式时，如果新参数与当前不同，就必须释放旧图、重捕新图；如果相同，则短路返回、不付重捕代价。`apply_mode` 的每一次调用，都可能触发一次（非 MTP）或两次（MTP，主图 + MTP 子图）重捕获。

#### 4.2.2 核心流程

每个 workload 的 `run` 在进入某个模式时，第一件事就是 `apply_mode`：

```text
workload.run(generator, modes)
  └── for mode in modes:
        apply_mode(generator, mode)                          # 切采样配置（可能重捕图）
        generator.generate(PROMPT, ..., with_mtp=mode.with_mtp)  # 用该模式跑数据
```

`apply_mode` 的下游链路：

```text
apply_mode(mode)
  └── generator.update_sampling_params(temperature, top_p, top_k, use_topp)
        └── decode_layer.update_sampling_config(...)        # ShowHandsDSALayer
              ├── 四元组判等 → 相等则 return（不重捕）
              └── 否则 go_home 释放旧图 → 改写 SAMPLING_CONFIG 槽 → prepare_money 重捕新图
```

注意：`with_mtp` 不参与采样四元组的判等（它不进 CUDA Graph 的采样配置），它只是作为参数透传给 `generate`，决定走 `_generate_with_mtp` 还是 `_generate_without_mtp`。

#### 4.2.3 源码精读

`apply_mode` 的全部实现就是把模式字段转发给生成器：

[tilert/benchmark/\_\_init\_\_.py:47-54](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L47-L54) —— `apply_mode` 调 `generator.update_sampling_params(...)`。注意它**没有传 `with_mtp`**：`with_mtp` 在每个 workload 的 `generate(...)` 调用里单独传（见例如 [tilert/benchmark/short_prompt.py:35](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/short_prompt.py#L35)）。

生成器侧的薄封装，记录参数后转发给 `decode_layer`：

[tilert/models/deepseek_v3_2/generator.py:138-152](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L138-L152) —— `DSAv32Generator.update_sampling_params`：先更新自身字段，再调 `self.decode_layer.update_sampling_config(...)`。

真正干活的是 `ShowHandsDSALayer.update_sampling_config`，也就是 u3-l4 精读过的「四元组判等短路 + go_home + prepare_money」：

[tilert/models/deepseek_v3_2/modules/end2end.py:253-260](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L253-L260) —— 四元组判等：`new_config == current_config` 直接 `return`，不重捕图。

[tilert/models/deepseek_v3_2/modules/end2end.py:267-276](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L267-L276) —— 不等时先 `dsa_show_hands_go_home` 释放旧图（MTP 模式释放两次：主图 + MTP 子图），再更新四个采样字段。

[tilert/models/deepseek_v3_2/modules/end2end.py:278-297](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L278-L297) —— 把新四元组写进每卡的 `SAMPLING_CONFIG` 槽，再对每卡 `dsa_show_hands_prepare_money` 重捕新图。

把它套回基准测试：在一次完整的 `run` 里，3 个模式依次 `apply_mode`，通常会产生 **2 次**真实重捕获——

- 模式 1（默认采样）→ 模式 2（同为默认采样）：四元组相等，**短路、不重捕**；
- 模式 2（默认）→ 模式 3（top-p）：四元组不等，**重捕**。

> 这也解释了为什么前两个模式刻意保持采样参数相同：只让 `with_mtp` 变化，避免采样切换本身的干扰，也省掉一次重捕获。

#### 4.2.4 代码实践

**目标**：在源码层面追踪一次完整网格里 `apply_mode` 会触发几次 CUDA Graph 重捕获。

**步骤**：

1. 假设默认 CLI（`--top-p 1.0`），按 4.1.3 列出 3 个模式的四元组。
2. 模拟 `apply_mode` 的调用序列：`mode1 → mode2 → mode3`，套用 [end2end.py:257-260](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L257-L260) 的判等规则。
3. 回答：哪一次 `apply_mode` 会打印 `Recapturing CUDA graphs: ...`（见 [end2end.py:262-265](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L262-L265)）？

**需要观察的现象**：在真实运行日志（待本地验证）里，每个 workload 的 `run` 开始时应能看到一次 `Recapturing CUDA graphs: ...`，且只发生在进入第三个模式时。

**预期结果**：每个 workload 触发 1 次重捕获（mode2→mode3 那次）；mode1→mode2 不触发。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `apply_mode` 不把 `with_mtp` 一起传给 `update_sampling_params`？

> **答案**：因为 `with_mtp` 不是采样参数，不进 `SAMPLING_CONFIG` 槽、不参与四元组判等、不触发重捕图。它只决定 `generate` 走哪条解码路径（`_generate_with_mtp` vs `_generate_without_mtp`），所以由 workload 在调用 `generate(..., with_mtp=mode.with_mtp)` 时单独传入。

**练习 2**：如果把第三个模式的 `top_k` 设成和前两个不同（例如 CLI 传 `--top-k 128`），重捕获次数会变吗？

> **答案**：不会变次数，仍是 1 次（mode2→mode3）。前两个模式不读 `args.top_k`、仍用默认 256，所以 mode1→mode2 依旧相等、短路。改变的只是重捕获那一刻打印出的 `top_k` 值。

---

### 4.3 汇总表统计

#### 4.3.1 概念说明

基准测试的产出是一张 Markdown 表格：每行块对应一个模式，每列对应一个 workload，每个单元格存 3 个指标。这 3 个指标由 `CellStats` 承载：

- **`tok/s`**：每秒产出多少 token（吞吐）。
- **`it/s`**（iteration/s）：每秒推进多少个解码步（与单步延迟成反比，更贴近 TileRT 的 TPOT 优化目标）。
- **`acc`**（acceptance rate）：MTP 模式独有的「平均/最小/最大接受长度」，非 MTP 模式为 `-`。

关键直觉：**在 MTP 下，一个解码步可能产出多个 token**，所以 `tok/s` 与 `it/s` 不再相等——这正是投机解码的价值所在（`tok/s > it/s`）。而在非 MTP 下，一步一个 token，两者数值上接近。

#### 4.3.2 核心流程

三类 workload 的统计口径不同，但都填同一个 `CellStats`：

```text
非 MTP（time_list 逐 token，accepted_counts 为空）:
  tok/s = 1 / mean(每 token 耗时)
  it/s  ≈ tok/s
  acc   = "-"

MTP（time_list 逐步，accepted_counts 为逐步接受数）:
  tok/s = 累计 token 数 / 累计耗时   （在检查点上取值）
  it/s  = 解码步数 / 累计耗时
  acc   = "均值/最小/最大" 接受长度
```

之后两步收尾：

- `merge_stats`：把各 workload 返回的 `BenchStats` 按**模式 label** 合并，让同一模式的不同列（`Short@200` / `Coding` / `Long`）拼到一行。
- `print_summary_table`：按 `[tok/s, it/s, acc]` 三个行标签把每个模式渲染成 3 行。

#### 4.3.3 源码精读

单元格的数据结构：

[tilert/benchmark/\_\_init\_\_.py:23-30](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L23-L30) —— `CellStats`：`tok_s` 是 float，`iters_s` 与 `acc_rate` 是已格式化的字符串（默认 `"-"`）。这解释了为什么非 MTP 单元格的 acc 列直接显示 `-`。

**短 prompt 的统计**（最复杂，因为它有 20 次迭代与检查点）：

[tilert/benchmark/short_prompt.py:65-95](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/short_prompt.py#L65-L95) —— MTP 分支：用 `np.cumsum(accepted_list)` 与 `np.cumsum(time_list)` 得到「累计 token / 累计耗时」曲线，在检查点 `token_num`（=200）处用 `searchsorted` 定位，取 `tok_count / elapsed` 为 tok/s，`(idx+1)/elapsed` 为 it/s；`acc_rate` 取所有步接受长度的「均值/最小/最大」。

[tilert/benchmark/short_prompt.py:96-108](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/short_prompt.py#L96-L108) —— 非 MTP 分支：直接 `speed = 1 / mean(time_list[:200])`，it/s 与 tok/s 同值，不填 acc_rate。

**编码 prompt 的统计**（单次生成，整体口径）：

[tilert/benchmark/coding_prompt.py:41-54](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/coding_prompt.py#L41-L54) —— MTP：`total_tokens / total_time` 得 tok/s，`len(time_list) / total_time` 得 it/s，acc 取「均值/最小/最大」；非 MTP（`elif time_list`）：`1 / mean(time_list)`。

**长 prompt 的统计**（单次长生成，并按 2048/512 切分前后段）：

[tilert/benchmark/long_prompt.py:48-70](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/long_prompt.py#L48-L70) —— 把累计 token 曲线在 `2048` 与 `2048+512` 处切开，分别算前段 `pre_ips` 与后段 `post_ips`，拼成 `"pre/post it/s"` 字符串。这是为了观察「长序列里 it/s 是否随上下文变长而衰减」。

**合并与打印**：

[tilert/benchmark/\_\_init\_\_.py:57-63](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L57-L63) —— `merge_stats`：对每个模式 label，`merged.setdefault(mode, {}).update(cols)`，把不同 workload 的列并入同一行。

[tilert/benchmark/\_\_init\_\_.py:70-101](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L70-L101) —— `print_summary_table`：先收集所有列名 `col_keys`，再把每个单元格格式化成 3 个字符串（`[_fmt(tok_s), iters_s, acc_rate]`）。

[tilert/benchmark/\_\_init\_\_.py:87](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L87) —— `ROW_LABELS = ["tok/s", "it/s", "acc"]`：这就是每个模式占 3 行的来源。表头与分隔行见 [tilert/benchmark/\_\_init\_\_.py:114-132](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L114-L132)。

最后，`generate.py` 把多个 workload 的结果合并并打印：

[tilert/generate.py:278-296](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L278-L296) —— 跑 workload 列表 → 取各自的 stats → `merge_stats` → `print_summary_table`，并附一张 `Loading / Benchmark / Total` 计时表。

#### 4.3.4 代码实践

**目标**：读懂汇总表每一格的含义。下面给出一个**无需 GPU** 的「源码阅读型实践」，再用一条真实命令做对照。

**步骤（源码阅读型）**：

1. 先假设一次 MTP 短 prompt 运行得到 `time_list = [t0, t1, ...]`、`accepted_counts = [2, 3, 1, 4, ...]`。
2. 套用 [short_prompt.py:65-80](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/short_prompt.py#L65-L80) 的算法：`cumsum_tokens = [2, 5, 6, 10, ...]`，在检查点 200 处用 `searchsorted` 找到首次 `≥200` 的位置 `idx`，于是 `tok/s = 200 / cumsum_times[idx]`。
3. 回答：如果 `it/s = 350` 而 `tok/s = 950`，这意味着平均每个解码步接受约多少个 token？

**真实运行（需 8×B200 与已转换权重，待本地验证）**：

```bash
python -m tilert.generate --model deepseek_v3_2 \
    --model-weights-dir /path/to/DeepSeek-V3.2-TileRT \
    --workloads short --modes top-k1 --max-new-tokens 1000
```

**需要观察的现象**：表格里 `top-k1 w/o MTP` 与 `top-k1 w/ MTP` 两行块在 `Short@200` 列下的对比——MTP 行的 `tok/s` 应明显高于非 MTP 行，且 MTP 行多出一个非空的 `acc`（形如 `2.77/1/4`）。

**预期结果**：MTP 行 `tok/s ÷ it/s` 约等于 `acc` 里的均值；非 MTP 行 `acc` 为 `-`，且 `tok/s ≈ it/s`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `short_prompt` 要做「1 次预热 + 20 次迭代」，而 `coding`/`long` 只跑 1 次？

> **答案**：`short_prompt` 的目标是**稳态吞吐**。第一次 `generate` 会触发 CUDA Graph 捕获、缓存填充等一次性开销（见 [short_prompt.py:34-35](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/short_prompt.py#L34-L35) 的 warmup），不计入统计；后 20 次才是稳态。`coding`/`long` 关注的是「在真实任务长度下的整体吞吐」，单次已足够。

**练习 2**：`long_prompt` 为什么要把 it/s 拆成 `pre/post` 两段（2048 前与 2048+512 后）？

> **答案**：为了观察**序列变长时单步延迟是否衰减**。NSA 稀疏注意力把开销钉在 `index_topk=2048`（见 [u2-l2](u2-l2-model-args-and-two-models.md)），但随着 KV 缓存增长，仍可能有轻微变化。拆成两段能直接读出这种趋势——若 `post_ips` 明显低于 `pre_ips`，说明长上下文下解码变慢。

**练习 3**：`merge_stats` 用 `dict.update` 合并各 workload 的列。如果两个 workload 恰好产出同一个列名，会发生什么？

> **答案**：后合并的会覆盖先合并的（`update` 语义）。当前三个 workload 的列名分别是 `Short@200` / `Coding` / `Long`，互不冲突，所以实际不会触发；但这是一个需要留意的约束——自定义 workload 时应保证列名唯一。

---

## 5. 综合实践

把本讲的三个模块串起来，完成一次「最小基准 + 改造 workload」的完整任务。

**任务**：运行一次最小基准，对照源码解释表格的每一个单元格；然后改造短 prompt 的内容，观察汇总变化。

**步骤**：

1. **裁剪网格**：阅读 [generate.py:250-276](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L250-L276)，确认 `--workloads short --modes top-k1` 会留下哪些模式（提示：`top-k1` 是 label 子串匹配，会同时命中 `top-k1 w/o MTP` 与 `top-k1 w/ MTP`，即留下前两个模式）。
2. **运行最小基准**（需 8×B200 与已转换权重，待本地验证）：

   ```bash
   python -m tilert.generate --model deepseek_v3_2 \
       --model-weights-dir /path/to/DeepSeek-V3.2-TileRT \
       --workloads short --modes top-k1 --max-new-tokens 600
   ```

3. **对照解释表格**：针对输出的 `Benchmark Summary`，逐格说明：
   - `Short@200` 列下 `top-k1 w/o MTP` 的 `tok/s` 来自 [short_prompt.py:96-108](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/short_prompt.py#L96-L108) 的 `1/mean` 公式；其 `acc` 为 `-`。
   - `top-k1 w/ MTP` 的 `tok/s` 来自 [short_prompt.py:65-80](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/short_prompt.py#L65-L80) 的累计曲线；其 `acc` 形如 `μ/min/max`。
4. **改造 workload**：编辑 [short_prompt.py:17](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/short_prompt.py#L17)，把 `PROMPT` 换成一个更长的指令（例如要求生成 5 段技术说明）。重新运行步骤 2。

**需要观察的现象**：步骤 4 之后，由于生成内容变长、单步负载变化，`Short@200` 列的 `tok/s` 可能略有不同；但 `acc`（接受长度）主要取决于 MTP 头本身，受 prompt 内容影响较小。

**预期结果**：你能口头解释表格中任意一个单元格的来源文件、行号与公式；并理解「prompt 内容影响的是单步耗时与输出，而非 MTP 的接受长度统计口径」。

> ⚠️ 本实践涉及修改 `tilert/benchmark/short_prompt.py`。本讲义仅作学习演示；按你的项目规范，请在单独的分支/副本上操作，不要污染主仓库源码。

---

## 6. 本讲小结

- 基准测试是「`BenchMode`（采样档位）× workload（prompt 与迭代策略）」的**网格**；默认跑 3 个模式（top-k1 w/o MTP、top-k1 w/ MTP、top-p0.95 w/ MTP）与 3 类 workload（short / coding / long）。
- `BenchMode` 是一份带默认值的采样配置 dataclass；前两个默认模式刻意保持采样参数相同、仅 `with_mtp` 不同，构成纯净对照，并省掉一次 CUDA Graph 重捕获。
- `apply_mode` 是模式与生成器之间的唯一桥梁：`apply_mode → update_sampling_params → update_sampling_config`，后者用四元组判等决定是否 `go_home` + `prepare_money` 重捕图（承接 u3-l4）。
- `with_mtp` 不进采样四元组、不触发重捕图，只在 `generate(...)` 时决定走哪条解码路径。
- 单元格三指标：`tok/s`（吞吐）、`it/s`（解码步频率，贴近 TPOT）、`acc`（MTP 接受长度 μ/min/max）。MTP 下 `tok/s` 用累计 token/累计耗时，非 MTP 下用 `1/mean(每 token 耗时)`。
- `merge_stats` 按模式 label 把各 workload 的列并入一行，`print_summary_table` 按 `[tok/s, it/s, acc]` 把每个模式渲染成 3 行；`--modes`/`--workloads` 分别按 label 子串与白名单做过滤。

## 7. 下一步学习建议

- **向深走（专家层）**：本讲的基准是「单进程、bs=1」的离线吞吐评估。生产环境的真实部署是 [u4 系列](u4-l1-pd-overview-and-profile.md) 的 **PD 分离架构**（vLLM prefill + TileRT decode）。建议接着读 [u4-l1 PD 分离架构总览](u4-l1-pd-overview-and-profile.md)，对比「离线基准的 bs=1 解码」与「在线 PD 的 prefill→decode 解耦」在延迟模型上的差异。
- **向回溯走**：若你对 `tok/s` 与 `it/s` 在 MTP 下为何分化的底层机制还不够清楚，建议重读 [u3-l3 MTP 多 token 预测与投机解码](u3-l3-mtp-speculative-decoding.md) 里的「平均接受长度 μ」与 `num_accepted` 推进游标的逻辑。
- **源码延伸**：本讲的 `PerStepData`/`PerStepDict`（[tilert/benchmark/\_\_init\_\_.py:35-44](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L35-L44)）记录了每次运行的逐步耗时，配合 CLI 的 `--tag` 参数可用于回归对比；可顺藤摸瓜阅读仓库内是否有对应的绘图/回归脚本（未在 `source_files` 中，待确认）。
