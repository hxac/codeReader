# E2E 训练集成与日志可观测性

## 1. 本讲目标

本讲是「测试、集成与扩展」单元的第三篇，回答两个在生产中最实际的问题：

1. **怎么把 FFPA 接进一个真实的大模型训练流程？** 我们以 README 给出的 NeMo Gemma4-31B 案例为锚点，讲清楚「为什么只替换其中 10/60 层 D=512 的全注意力层，其余继续走 SDPA」这条**选择性集成**策略。
2. **集成之后怎么观察 FFPA 到底在干什么？** 我们精读 `src/ffpa_attn/logger.py` 这一套轻量日志工具，掌握日志等级、`once` 去重、多卡 rank0 过滤三个机制。

学完后你应该能够：

- 说清「只替换大 D 全注意力层」背后的工程与算法依据，并能复述 NeMo Gemma4-31B 案例的关键数字。
- 知道 `FFPA_LOGGER_LEVEL`、`FFPA_FORCE_ONLY_RANK0_LOGGING` 两个环境变量的作用与设置时机。
- 理解 `init_logger`、`_log_once`、`Rank0Filter` 三者的实现，明白 `info_once`/`debug_once`/`warning_once` 的去重粒度。
- 能用 DEBUG 日志定位一次前向里 FFPA 是否命中了持久化调优配置。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（它们来自前置讲义，这里只做一句话回顾）：

- **head_dim（D）与 FFPA 的适用边界**（u1-l1）：FFPA 主攻 prefill + 大 head_dim（D>256）+ 长序列（N≥512）；当 D≤256 或 N<512 时未必比 SDPA 快，会自动回退（fallback）到 SDPA。
- **monkey-patch 接入**（u1-l4）：`F.scaled_dot_product_attention = ffpa_attn_func` 一行替换后，FFPA 内部会先做 `fallback()` 短路判定，不支持的形状静默回退到原生 SDPA。
- **公共 API 签名**（u2-l1）：`ffpa_attn_func(query, key, value, attn_mask, dropout_p, is_causal, scale, enable_gqa, **kwargs)`，返回 `[B, Nh_q, Nq, D]` 的输出。

此外需要一点 Python 标准库 `logging` 的常识：`logging.getLogger(name)` 按点分名字构成树形层级；`Handler` 决定日志去哪（控制台/文件）；`Filter` 决定一条日志要不要被丢弃；`Formatter` 决定输出文本长什么样。本讲的 `logger.py` 全部建立在这套标准机制之上，没有引入第三方依赖。

> 术语提示：**rank** 指分布式数据并行（DDP）/FSDP 训练里每个进程对应的设备编号，rank 0 通常被约定为「主进程」。**rank0 过滤**就是只让 rank 0 打日志、其余 rank 静默。

## 3. 本讲源码地图

本讲只涉及三个文件，职责非常聚焦：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md) | 顶层文档。本讲用到两段：Quick Start 的 monkey-patch 示例（46-54 行）与 End-to-End (E2E) Training 章节（124-130 行）。 |
| [src/ffpa_attn/logger.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py) | FFPA 的包级日志工具，全文件仅 192 行。定义了日志等级解析、共享 handler、`_log_once` 去重、`Rank0Filter`，并把 `info_once`/`debug_once`/`warning_once` 三个方法 monkey-patch 到 `logging.Logger` 上。 |
| [src/ffpa_attn/ffpa_attn_interface.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py) | 公共入口。本讲关注它如何 `init_logger(__name__)` 取得一个 logger，以及模块 docstring 里关于 SDPA fallback 应发 `warning_once` 的设计说明。 |

另外，作为「真实在用 `*_once`」的活样本，我们会顺带引用运行时配置查找模块 [src/ffpa_attn/triton/_persistent_autotune.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py) 里的一处 `logger.debug_once` 调用——它是实践任务里能直接观察到的日志来源。

## 4. 核心概念与源码讲解

### 4.1 E2E 训练集成策略：选择性替换大 D 全注意力层

#### 4.1.1 概念说明

把一个新 kernel 库接进真实训练，最朴素的想法是「全部注意力层都换掉」。但 FFPA 的设计定位决定了这不是最优解：它只在 **prefill + 大 head_dim + 长序列** 这个交集里比 SDPA 快，出了这个集合要么不快、要么直接回退。因此正确的集成姿势是 **选择性替换**——只把模型里「FFPA 真正擅长」的那些注意力层换成 `ffpa_attn_func`，其余层保持原样（继续用 SDPA），让两类实现各司其职。

README 的 E2E 章节用 NVIDIA-NeMo Automodel 的一个真实 PR 给出了这条策略的量化证据：在 Gemma4-31B 训练上，只加速其中一部分注意力层，就拿到了端到端（E2E）吞吐提升。

#### 4.1.2 核心流程

选择性集成的判断流程可以这样概括：

1. **盘点模型的注意力层**：列出每一层的 head_dim、序列长度、是否全注意力（full attention）还是滑动窗/局部注意力等变体。
2. **按 FFPA 适用边界筛选**：保留满足「D>256 且 prefill 长序列」的全注意力层作为替换目标；其余层（小 D、短序列、非全注意力）继续走 SDPA。
3. **落地替换**：在目标层的 forward 里把对 SDPA 的调用换成 `ffpa_attn_func`（或干脆 monkey-patch 全局 SDPA 符号，靠 FFPA 自身的 `fallback()` 把不该接管的形状再退回 SDPA）。
4. **校验**：对比替换前后的 loss 曲线与显存占用，确认数值在 bf16 噪声范围内、显存无明显膨胀。

Gemma4-31B 案例里，模型共有 **60** 个注意力层，其中只有 **10** 层是 D=512 的全注意力层——这正是被 FFPA 加速的那 10 层（即「10/60」）。其余 50 层不在 FFPA 的甜点区，保留 SDPA。最终在 8xH200、序列长 8192、FSDP2 + Activation Checkpointing 的配置下，E2E 吞吐约 **1.4x~1.5x** 于纯 SDPA，且显存占用相当、loss 落在正常 bf16 噪声内。

#### 4.1.3 源码精读

README 的 E2E 章节是本模块的唯一权威出处，原文如下：

[README.md:124-130](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L124-L130) —— End-to-End (E2E) Training 章节，给出了 NeMo Automodel PR #2436 的结论：Gemma4-31B、L=8192、8xH200、FSDP2 + Activation Checkpointing，加速 10/60 个 D=512 全注意力层，E2E 吞吐约 1.4x~1.5x，显存相当，loss 在 bf16 噪声内。

把这条结论与前置讲义的适用边界对齐看，就能解释「为什么是 10 层而不是 60 层」：

- **D=512 落在 FFPA 的甜点区**（D>256），Split-D 的 O(1) SRAM 优势才发挥得出来（见 u1-l1、u4-l2）。
- **其余 50 层不替换**，是因为它们要么 head_dim 较小（D≤256 时 FFPA 默认就走 `fallback()` 回退 SDPA，见 u1-l4、u3-l3），要么是非全注意力变体（如滑动窗），不在 FFPA 当前支持范围内。强行替换只会触发回退或报错，徒增开销与风险。
- **显存相当**是因为 Split-D 把 D 方向的压力从 SRAM 转移到寄存器，激活张量形状不变，没有引入额外的大块显存。

而 monkey-patch 那条「一行接入」的姿势在 Quick Start 里已经给出：

[README.md:46-54](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L46-L54) —— 把 `F.scaled_dot_product_attention` 指向 `ffpa_attn_func`，注释明确写道「FFPA 不支持的会自动回退到 SDPA：D≤256 等」。这正是「全局替换 + 依赖 FFPA 自身 fallback」的集成姿势，与「只替换 10 层」的精确替换姿势互为补充。

#### 4.1.4 代码实践

**实践目标**：理解「选择性替换」与「全局 monkey-patch + 自动回退」两种姿势的关系。

**操作步骤**：

1. 阅读 README 的 E2E 章节与 Quick Start，确认两段文字。
2. 对照下方的「示例代码」（非仓库代码，仅作说明），理解两种集成姿势的差异。

```python
# 示例代码：两种集成姿势对比（非项目原有代码）

# 姿势 A：全局 monkey-patch + 自动回退（README Quick Start）
# import torch.nn.functional as F
# from ffpa_attn import ffpa_attn_func
# F.scaled_dot_product_attention = ffpa_attn_func   # 一行接入
# → 60 层全部经过 ffpa_attn_func，但小 D / 短序列会在 fallback() 里退回 SDPA

# 姿势 B：精确替换（NeMo Gemma4-31B 案例采用）
# 只在 10 个 D=512 全注意力层的 forward 内部调用 ffpa_attn_func，
# 其余 50 层继续走模型自带的 SDPA，完全不经过 FFPA。
```

**需要观察的现象**：两种姿势在数值上应等价（因为超出适用边界的层最终都落到 SDPA），但姿势 B 避免了「不需要 FFPA 的层也走一遍 fallback 判定」的轻微开销，也更利于在日志里精确区分哪些层真正用了 FFPA kernel。

**预期结果**：能用自己的话说出「10/60」里那 10 层被选中的判据是 head_dim=512（落在 D>256 的甜点区），而非随意挑选。

#### 4.1.5 小练习与答案

**练习 1**：假设某个模型所有注意力层都是 D=128，能否靠 monkey-patch FFPA 拿到 E2E 加速？为什么？

> **参考答案**：基本不能。D=128≤256，`ffpa_attn_func` 的 `fallback()` 会把这些形状短路回原生 SDPA（见 u1-l4、u3-l3）。即便全局替换，每一层最终都退回 SDPA，没有 kernel 层面的收益，反而多了一次 fallback 判定的开销。这也印证了 README「D≤256 时未必比 SDPA 快」的说明。

**练习 2**：为什么 NeMo 案例强调「显存占用与 SDPA 相当（similar memory footprint）」是一个重要结论？

> **参考答案**：训练里显存往往是能否跑起来的硬约束。如果换 kernel 导致激活或中间张量显存上涨，可能就要降 batch 或开更多 checkpointing，抵消速度收益。FFPA 的 Split-D 把 SRAM 工作集压到与 D 无关的 O(1)、输出张量形状与 SDPA 一致，因此显存几乎不变——这说明速度提升是「净赚」而非用显存换来的。

---

### 4.2 init_logger 与 FFPA 包日志体系

#### 4.2.1 概念说明

`logger.py` 的目标是给整个 `ffpa_attn` 包提供一套**统一外观、统一等级、可跨模块复用**的控制台日志，且不污染用户自己的 `logging` 配置。它的核心设计有三点：

1. **单一共享 handler**：所有模块通过 `init_logger(__name__)` 拿到的 logger，都挂同一个 `_default_handler`（输出到 stdout、带 `[FFPA]` 前缀）。这样无论哪个模块打日志，外观一致。
2. **`propagate = False`**：FFPA 的日志不会冒泡到用户应用的 root logger，避免被用户的 handler 重复打印或被全局 level 截断。
3. **等级由环境变量驱动**：日志等级从 `FFPA_LOGGER_LEVEL` 读取，默认 `INFO`，可在不改代码的情况下调试。

#### 4.2.2 核心流程

日志体系的初始化与使用流程：

1. `logger.py` 被 import 时，模块末尾的 `_setup_logger()` 立即执行一次，创建 `_default_handler` 并挂到根 logger `"FFPA"` 上。
2. 任意子模块（如 `ffpa_attn_interface.py`）调用 `init_logger("ffpa_attn.ffpa_attn_interface")`：
   - 再次调用 `_setup_logger()`（幂等，handler 只创建一次）；
   - `logging.getLogger(name)` 取得该名字的 logger；
   - 设等级、关 propagate、把共享 `_default_handler` 挂上去；
   - 返回这个 logger。
3. 之后该模块用 `logger.info(...)` / `logger.debug_once(...)` 打日志，经共享 handler 的 `NewLineFormatter` 格式化、`Rank0Filter` 过滤后输出到 stdout。

#### 4.2.3 源码精读

环境变量与格式常量：

[logger.py:16-20](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L16-L20) —— 定义 `FFPA_LOGGER_LEVEL`（等级）、`FFPA_FORCE_ONLY_RANK0_LOGGING`（rank0 过滤）两个环境变量名，以及统一的 `[%(asctime)s] [FFPA] %(message)s` 输出格式。

等级解析：

[logger.py:27-34](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L27-L34) —— `_log_level_from_env()` 读取 `FFPA_LOGGER_LEVEL`（默认 `INFO`），用 `getattr(logging, level_name, logging.INFO)` 把字符串名（如 `"DEBUG"`）映射为 `logging` 模块的数字常量；非法名字兜底回 `INFO`。**关键细节**：环境变量在 import 时就被读取，所以要让 DEBUG 生效，通常需要在启动 Python 进程前就设好该变量。

模块级状态：

[logger.py:22-24](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L22-L24) —— `_root_logger = logging.getLogger("FFPA")` 是包级根 logger；`_default_handler` 是被所有子 logger 共享的单个 handler；`_log_once_messages` 是去重集合（见 4.3）。

handler 装配：

[logger.py:79-100](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L79-L100) —— `_setup_logger()` 的关键：它先在现有 handlers 里找带 `_ffpa_default_handler` 标记的那个（保证幂等），找不到才新建一个 `StreamHandler(sys.stdout)`；随后给它装上 `NewLineFormatter` 和 `Rank0Filter`，并把 handler 自身 level 也设成环境等级。注意 handler 与 logger 的 level 都要设，因为 `logging` 取二者中更严的那个生效。

多行前缀对齐：

[logger.py:46-59](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L46-L59) —— `NewLineFormatter` 让多行日志的每一行续行都带上 `[时间] [FFPA]` 前缀，避免长消息换行后视觉上错位。

对外入口：

[logger.py:167-180](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L167-L180) —— `init_logger(name)`：调用 `_setup_logger()`，`getLogger(name)`，设等级、关 propagate，把共享 handler 挂到这个 logger 上（若尚未挂），返回它。

import 时的自启动：

[logger.py:183](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L183) —— 模块末尾裸调用 `_setup_logger()`，保证只要 `ffpa_attn.logger` 被 import（哪怕是间接 import），共享 handler 就已就绪。

调用方实例：

[ffpa_attn_interface.py:66-68](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L66-L68) —— 公共入口模块 `from .logger import init_logger` 后 `logger = init_logger(__name__)`，拿到名为 `ffpa_attn.ffpa_attn_interface` 的 logger。它就是模块 docstring 里提到的那个 logger。

#### 4.2.4 代码实践

**实践目标**：验证 `FFPA_LOGGER_LEVEL` 对输出等级的控制，并体会「必须在 import 前设好」这一时机约束。

**操作步骤**：

1. 在启动 Python **之前**设置环境变量（这样 import 时 `_log_level_from_env()` 才读到 DEBUG）：

```bash
# 待本地验证：在装好 ffpa-attn 的环境里执行
FFPA_LOGGER_LEVEL=DEBUG python -c "import ffpa_attn; print('imported')"
```

2. 对比不设该变量时的输出差异。

**需要观察的现象**：开 DEBUG 后，凡是 `logger.debug(...)` / `logger.debug_once(...)` 级别的消息（如持久化调优查找日志，见 4.4 实践）才可能出现在 stdout；默认 INFO 下这些 DEBUG 消息被静默。

**预期结果**：能确认「环境变量驱动等级」生效；若设了 DEBUG 却看不到任何 FFPA 日志，通常是因为当前调用路径根本没触发任何日志点（例如没有走到持久化配置查找）。**待本地验证**：具体是否打印取决于是否触发了日志调用点。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_setup_logger()` 里既给 logger 设 level，又给 handler 设 level？

> **参考答案**：Python `logging` 的生效等级是 logger 与 handler 二者的「交集」——一条记录必须同时通过 logger 的 level 门槛和 handler 的 level 门槛才会输出。若只设 logger 不设 handler，handler 默认 `NOTSET`（0，放行一切），看似没问题；但若只设 handler 不设 logger，logger 默认 `WARNING`，会把 DEBUG/INFO 直接拦在 logger 层、根本到不了 handler。两边都设成环境等级，才能保证行为与 `FFPA_LOGGER_LEVEL` 完全一致。

**练习 2**：`init_logger` 给每个模块的 logger 都挂上同一个 `_default_handler`，会不会导致同一条消息被打印多次？

> **参考答案**：不会。虽然多个 logger 共享一个 handler 实例，但每条日志只从「发出它的那个 logger」流出一次；又因为 `propagate=False`，它不会冒泡到祖先 logger 再被打印。共享的是 handler 对象，不是消息流。

---

### 4.3 _log_once 与 info_once / debug_once / warning_once 去重

#### 4.3.1 概念说明

注意力 kernel 在训练里会被调用**成千上万次**（每个 step、每一层、每个 micro-batch 都会调）。如果每次回退、每次配置查找都打一条日志，输出会被淹没、IO 也会拖慢训练。因此 FFPA 提供了「**只打一次**」语义的三个方法：`info_once` / `debug_once` / `warning_once`——同一条消息在一个进程内只输出第一次，之后静默。

这三个方法不是 `logging` 自带的，而是 `logger.py` 在 import 时 monkey-patch 到 `logging.Logger` 类上的，所以任何 `logging.Logger` 实例（包括 FFPA 自己的 logger）都能直接调用 `logger.warning_once(...)`。

#### 4.3.2 核心流程

去重的判定流程：

1. 调用 `logger.warning_once(fmt, *args)` 时，先进入 `_log_once`。
2. `_render_message` 用 `fmt % args`（即 `%` 插值）**把参数渲染成最终文本**。
3. 以 `(logger.name, level, 渲染后的文本)` 三元组为 key，查模块级集合 `_log_once_messages`：
   - 已存在 → 直接 `return`（不打印）；
   - 不存在 → 加入集合，再调用 `logger.log(level, fmt, *args)` 真正打印一次。

**关键点**：去重 key 用的是**渲染后的文本**，不是模板。所以 `logger.debug_once("D=%d", 256)` 和 `logger.debug_once("D=%d", 512)` 是两条不同的消息，**各打印一次**；只有完全相同的渲染文本才被合并。

#### 4.3.3 源码精读

去重集合：

[logger.py:24](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L24) —— `_log_once_messages: set[tuple[str, int, str]]`，进程级全局集合，元素是 `(logger 名, 等级, 渲染文本)`。

渲染消息（先插值再判重）：

[logger.py:103-122](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L103-L122) —— `_render_message` 构造一个临时 `LogRecord` 并调 `.getMessage()`，等价于执行一次 `msg % args`，得到带参数的最终字符串。这一步是为了让判重落在「含参数的最终文本」上。

核心去重逻辑：

[logger.py:125-141](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L125-L141) —— `_log_once`：算 key → 查集合 → 命中则 `return`，否则加入集合后 `logger.log(level, msg, *args, **kwargs)`。注意它把**原始模板 `msg` 与 `args`** 传给 `logger.log`（让标准 logging 再渲染一次用于真正输出），而不是传渲染好的字符串——这样标准 logging 的格式化链路（如 `%` 插值异常 traceback）保持一致。

三个方法与 monkey-patch 挂载：

[logger.py:144-164](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L144-L164) —— 定义 `_info_once` / `_debug_once` / `_warning_once`，分别绑定 `logging.INFO/DEBUG/WARNING`；随后三行赋值把它们装到 `logging.Logger` 类上，使所有 logger 实例都能用。

一个「设计意图」与「当前实现」的差异说明：公共入口模块的 docstring 描述了 dense 路径在硬件/head_dim 不匹配回退 SDPA 时，应在这个 logger 上发一条 `warning_once`：

[ffpa_attn_interface.py:46-51](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L46-L51) —— docstring 说明：dense 入口的纯硬件/head_dim 不匹配会回退 SDPA，并在「未显式强制后端」时发一条 `warning_once`；其余约束（dtype、dropout_p>0、显式 attn_mask）则直接抛异常，不静默回退。

> 说明：在本讲分析的 HEAD（`882989c`）上，dense 入口的这条 `warning_once` 调用点在源码中**未见实际接入**（docstring 描述的是设计意图）；当前在运行时热路径上**真实生效**的 `*_once` 调用，是持久化调优查找里的 `logger.debug_once`，见下面 4.4 节的活样本。学习时应把「机制（logger.py 里完整实现）」与「调用点（各模块实际使用）」分开看。

#### 4.3.4 代码实践

**实践目标**：用最小例子亲眼看一次「只打一次」的效果，并验证「渲染文本不同则各打一次」。

**操作步骤**：

```python
# 示例代码：可直接在装好 ffpa-attn 的环境里运行（待本地验证）
import logging
from ffpa_attn import logger as _  # 触发 logger.py 的 import，挂上 *_once 方法
log = logging.getLogger("demo")
log.setLevel(logging.DEBUG)
log.addHandler(logging.StreamHandler())

for _ in range(5):
    log.warning_once("fallback to SDPA at D=%d", 512)   # 模板+参数相同
log.warning_once("fallback to SDPA at D=%d", 256)       # 渲染文本不同
```

**需要观察的现象**：第一条消息只打印一次（尽管循环 5 次）；第二条因为渲染文本 `...D=256` 与 `...D=512` 不同，会再打印一次。总共打印 2 条。

**预期结果**：输出两条 `... fallback to SDPA at D=512`（仅 1 条）与 `... fallback to SDPA at D=256`（1 条）。**待本地验证**：取决于 `logging` 与 `ffpa_attn.logger` 的具体版本行为。

#### 4.3.5 小练习与答案

**练习 1**：如果用 `logger.warning_once("hit %s", layer_name)`，而 `layer_name` 每层都不同，会发生什么？这符不符合「只警告一次」的初衷？

> **参考答案**：每个不同的 `layer_name` 都会产生不同的渲染文本，于是每层各打印一次——退化成普通 `warning`。这通常**不符合**初衷：本意是「同一类事件只报一次」。若真想「整个进程只报一次」，应把变化的参数从模板里去掉（例如只打 `"fallback to SDPA"`，或把层名降到 DEBUG 级别单独打）。这正体现了「去重粒度在渲染文本」这一设计的关键影响。

**练习 2**：`_log_once` 为什么不直接 `logger.log(...)` 然后靠 `logging` 自己去重？

> **参考答案**：标准 `logging` 没有「按消息内容去重」的内建机制（`Filter` 可以丢弃，但要自己写判重逻辑）。FFPA 用一个模块级集合显式记录「已打过的 (name, level, text)」，把去重做成一行 `if key in set: return`，既轻量又精确到渲染文本。

---

### 4.4 Rank0Filter：多卡训练的 rank0 日志过滤

#### 4.4.1 概念说明

在 DDP/FSDP 多卡训练里，每个 rank（进程）都会独立执行同一份模型代码，也就都会独立触发同一条日志。`_log_once` 的去重集合是**进程级**的，所以它只能保证「每个 rank 各打一次」——8 卡就是 8 条重复的 `fallback to SDPA`，照样刷屏。

`Rank0Filter` 解决的就是这最后一层重复：它是一个挂在共享 handler 上的 `logging.Filter`，在 **opt-in**（由环境变量开启）的前提下，只放行 rank 0 的日志、丢弃其余 rank 的，从而把 N 条重复压成 1 条。

#### 4.4.2 核心流程

过滤判定流程（在 handler emit 时触发）：

1. 检查 `FFPA_FORCE_ONLY_RANK0_LOGGING` 是否为 truthy（`1/true/yes/on`）。
   - **否** → 直接返回 `True`（放行所有 rank，即默认行为：每个 rank 都能打）。
   - **是** → 进入下一步。
2. 检查 `torch.distributed` 是否可用、是否已初始化、当前 `get_rank()` 是否不为 0。
   - 三者同时成立（说明是已初始化的分布式训练、且当前不是 rank 0）→ 返回 `False`（丢弃）。
   - 否则（单卡、未初始化、或就是 rank 0）→ 返回 `True`（放行）。

设计上之所以默认**不开** rank0 过滤，是因为调试时有时恰恰需要看每个 rank 各自的日志（例如排查某个 rank 数值异常）。只有确认为重复噪声、想净化输出时，才显式开启。

#### 4.4.3 源码精读

truthy 判定工具：

[logger.py:37-43](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L37-L43) —— `_truthy_env(name)`：把环境变量值小写后判断是否属于 `{1, true, yes, on}`，统一了「开关」的取值语义。

Rank0Filter 本体：

[logger.py:62-76](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L62-L76) —— `Rank0Filter.filter()`：先看环境变量是否要求 rank0-only；若没要求，`return True` 放行；若要求，则返回 `not (dist.is_available() and dist.is_initialized() and dist.get_rank() != 0)`。三个条件用 `and` 串联意味着：只要分布式未初始化（比如单卡或推理脚本），即使开了环境变量也不会误杀日志。

挂载到共享 handler：

[logger.py:96-100](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L96-L100) —— `_setup_logger()` 里，只要 handler 的 filters 中还没有 `Rank0Filter` 实例，就 `addFilter(Rank0Filter())`。因为所有子 logger 共享同一个 handler，所以**一次挂载，全局生效**。

一个值得注意的交互细节：`Rank0Filter` 在 handler 层过滤，而 `_log_once` 的去重在 logger 层（且发生在 `logger.log` 调用**之前**）。这意味着在非 rank0 进程上，`_log_once` 仍会先把 key 写进集合、再调用 `logger.log`，然后被 `Rank0Filter` 在 handler 层丢弃。结果是：非 rank0 进程「消耗了一次配额但没真正打印」。对「整个分布式作业只看到一条」这个目标而言这是正确的——rank 0 第一次调用时打印，非 rank0 即便先调用也只是静默消耗配额，最终全局只输出一条。

运行时真实调用点（活样本）：持久化调优查找里的 `debug_once`：

[_persistent_autotune.py:555-583](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L555-L583) —— `_debug_lookup_message` 用 `logger.debug_once(...)` 打印持久化配置查找的事件，字段包括 direction/kernel/dtype/D/Nq/Nkv/config 等。这是当前运行时热路径上**真实生效**的 `*_once` 调用，也是 4.4.4 实践里能直接观察到的日志来源。

[_persistent_autotune.py:757-764](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L757-L764) —— 查找的三态输出：命中缓存（cache hit）、首次选中配置（selected config）、未找到（lookup miss，回退默认 config）。配合 `logger.isEnabledFor(logging.DEBUG)` 守卫，未开 DEBUG 时完全跳过，零开销。

#### 4.4.4 代码实践

**实践目标**：用 DEBUG 日志观察一次 FFPA 前向里「持久化调优配置查找」的三态之一，并理解它与 rank0 过滤的关系。

**操作步骤**：

1. 在装好 ffpa-attn（含 Triton 后端）的环境里，启动前设置环境变量：

```bash
# 待本地验证：需要可用 GPU 与已生成的持久化 config（或观察 lookup miss）
FFPA_LOGGER_LEVEL=DEBUG python - <<'PY'
import torch
from ffpa_attn import ffpa_attn_func
# 大 D、长序列，落在 FFPA 甜点区，才会真正进入 Triton 前向 → 触发持久化查找
q = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda")
k = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda")
v = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda")
o = ffpa_attn_func(q, k, v)
print("output shape", tuple(o.shape))
PY
```

2. 若在多卡脚本里运行，再叠加 `FFPA_FORCE_ONLY_RANK0_LOGGING=1`，对比开/关时的日志条数。

**需要观察的现象**：

- 开 DEBUG 后，应能看到形如 `[时间] [FFPA] Persistent autotune cache hit: ...` 或 `... lookup miss: ...` 的 `debug_once` 输出（取决于是否已生成设备 config，详见 u8-l3）。
- 同一进程内即便前向调用多次，该消息只出现一次（`debug_once` 去重）。
- 多卡下开启 `FFPA_FORCE_ONLY_RANK0_LOGGING=1` 后，整批进程只输出一条（rank0 过滤）。

**预期结果**：能确认 DEBUG 等级打开后 `debug_once` 消息可见，并理解它经过了「logger 层 once 去重 → handler 层 Rank0Filter 过滤」两层处理。**待本地验证**：是否有 config 命中取决于是否运行过 `python -m ffpa_attn.autotune`（见 u8-l2）；无 GPU 环境下本脚本无法运行。

#### 4.4.5 小练习与答案

**练习 1**：在 8 卡 DDP 训练里，不开 `FFPA_FORCE_ONLY_RANK0_LOGGING`，一条 `warning_once` 最终会被打印几次？为什么？

> **参考答案**：8 次。`_log_once` 的去重集合是**进程级**的，8 个 rank 各自独立，每个 rank 第一次触发都会打印一次（之后该 rank 静默）。要压成 1 条，需要开 `FFPA_FORCE_ONLY_RANK0_LOGGING=1`，让 `Rank0Filter` 在 handler 层丢弃非 rank0 的输出。

**练习 2**：`Rank0Filter` 为什么要先判断 `dist.is_initialized()`，而不是直接 `get_rank() != 0`？

> **参考答案**：在分布式**未初始化**时（单卡训练、纯推理脚本、或调用时尚未 `init_process_group`），`dist.get_rank()` 会抛异常或无意义。用 `dist.is_available() and dist.is_initialized() and ...` 短路前置两个守卫，能保证非分布式场景下即使误开了环境变量，日志也不会被误杀（此时 `and` 链为假，`not 假` 为 `True`，放行）。

## 5. 综合实践

把本讲的三块知识串起来，完成下面这个「集成 + 可观测」的小任务。

**背景**：你要在一个假想的多卡训练脚本里接入 FFPA，目标是只让大 D 全注意力层走 FFPA，并且能在日志里确认「这些层确实进了 FFPA kernel、其余层回退 SDPA」。

**任务**：

1. **集成策略**：写一段说明（文字即可），参照 NeMo Gemma4-31B「10/60」案例，说明你会如何在一个「含若干 D=512 全注意力层 + 若干 D=128 层」的模型里选择替换目标，并解释为什么不对 D=128 层做任何改动。
2. **接入姿势**：给出两种接入写法的伪代码——(a) 全局 monkey-patch SDPA（依赖 FFPA 自身 fallback）；—精确替换（只改 D=512 层的 forward）。指出在「想精确知道哪些层用了 FFPA」时哪种更优。
3. **可观测**：设计你的调试环境变量组合：想在单进程里看持久化调优查找细节用哪个变量？想在 8 卡作业里把重复日志压成一条，再加哪个变量？说明这两个变量各自的设置时机（启动前 vs 任意时刻）。
4. **验证**：描述你会如何用 `FFPA_LOGGER_LEVEL=DEBUG` 的一次前向，结合 `debug_once` 的三态输出（cache hit / selected config / lookup miss），判断某层是否真的命中了调优配置。

**参考要点**：

- 替换目标 = 满足 D>256 且 prefill 长序列的全注意力层；D=128 层不替换（替换也会被 `fallback()` 退回 SDPA，见 u1-l4、u3-l3）。
- 想精确区分层 → 用姿势 (b) 精确替换，日志更干净；姿势 (a) 简单但所有层都过一遍 fallback 判定。
- 单进程看查找细节 → `FFPA_LOGGER_LEVEL=DEBUG`（启动前设，因 import 时读取）；8 卡压重复 → 叠加 `FFPA_FORCE_ONLY_RANK0_LOGGING=1`。
- 三态：`cache hit` 表示命中 `@lru_cache`（见 u8-l3）、`selected config` 表示首次从设备 JSON 选中、`lookup miss` 表示无配置回退默认 config（见 u8-l3）。

## 6. 本讲小结

- **选择性集成**是 FFPA 落地真实训练的正确姿势：只替换模型里 D>256 的全注意力层，其余层保持 SDPA。NeMo Gemma4-31B 案例（10/60 层 D=512、8xH200、L=8192）实证 E2E 吞吐约 1.4x~1.5x、显存相当、loss 在 bf16 噪声内。
- `logger.py` 用**单一共享 handler + `propagate=False`** 给整个包提供统一外观、不污染用户 logging；等级由 `FFPA_LOGGER_LEVEL` 驱动，import 时读取，默认 INFO。
- `_log_once` 实现 `info_once`/`debug_once`/`warning_once`，去重 key 是 `(logger 名, 等级, 渲染后文本)` 三元组——**粒度在最终文本而非模板**，参数不同的消息各打一次。
- `Rank0Filter` 是挂在共享 handler 上的 opt-in 过滤器，配合 `FFPA_FORCE_ONLY_RANK0_LOGGING=1` 把多卡的 N 条重复压成 rank0 的一条；默认关闭以保留按 rank 调试能力。
- 当前运行时热路径上真实生效的 `*_once` 调用是持久化调优查找的 `logger.debug_once`（三态：cache hit / selected config / lookup miss）；公共入口 docstring 描述的 dense 回退 `warning_once` 在本 HEAD 属设计意图、未见实际接入。
- 三层处理顺序：logger 层 `once` 去重 → handler 层 `Rank0Filter` 过滤 → `NewLineFormatter` 格式化输出到 stdout。

## 7. 下一步学习建议

- 想深入「持久化调优配置查找」的三态细节与就近匹配，直接读 [src/ffpa_attn/triton/_persistent_autotune.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py)，对应大纲 u8-l3。
- 想看「全局 monkey-patch + 不递归回退」如何被测试锁定，读 `tests/test_monkey_patch.py`，对应大纲 u9-l1。
- 准备自己动手扩展（新增后端或 head_dim）时，回顾公共入口与分发层的接线点，对应大纲 u9-l4。
- 若关心 FFPA 在哪些形状会回退 SDPA，回顾 `FFPAAttnMeta.fallback()` 的判定条件，对应大纲 u3-l3。
