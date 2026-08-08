# u6-l1 定制化接口总览：21+ 个 function-path hook

## 1. 本讲目标

slime 能用一套框架同时跑数学题、工具调用、沙箱执行、多智能体等截然不同的 RL 任务，靠的**不是**为每类任务 fork 一份代码，而是提供一组「插入点」（hook）。本讲学完后，你应该能够：

1. 看懂 slime 暴露的全部 `--xxx-path` 定制化接口（共 21+ 个），知道每个接口插在闭环的哪个阶段、默认实现是什么。
2. 理解四个**主接口**的层级关系：`--data-source-path`（原料）、`--rollout-function-path`（整条流水线）、`--custom-generate-function-path`（单件生成工位）、`--custom-rm-path`（打分工位）。
3. 分清 `--dynamic-sampling-filter-path` / `--buffer-filter-path` / `--rollout-sample-filter-path` / `--rollout-sample-hook-path` 这几个「过滤/钩子」接口各自过滤的对象与触发时机。
4. 读懂 `load_function` 如何把一个 import path 字符串（如 `"slime.rollout.sglang_rollout.generate_rollout"`）解析成真正的函数对象。
5. 会用 `tests/plugin_contracts/` 下的 CPU 契约测试，在不需要 GPU 的情况下自检你写的自定义实现是否符合签名与返回结构。

本讲是 U6（定制化接口与 RL 算法）的**导览课**，只建立全景图与底层加载机制；具体某个接口（如 custom-generate、custom-rm、custom-loss）的写法细节留待 u6-l2 ~ u6-l5 展开。

## 2. 前置知识

阅读本讲前，你需要先建立以下认知（对应前置讲义）：

- **slime 是「采样→训练→权重同步」的闭环**（u1-l1、u2-l1）：rollout 产 Sample（含 reward）→ data buffer 桥接 → training 消费 → 权重同步回 rollout。
- **Sample 是贯穿全框架的核心数据载体**（u3-l1）：它有 `tokens`、`response`、`loss_mask`、`reward`、`status`、`rollout_id` 等字段，本讲涉及的几乎所有 hook 都在「拿一个/一组 Sample 进来处理、再放回去」。
- **默认 rollout 函数 `generate_rollout` 的执行流程**（u3-l2）：取 prompt → 调 SGLang 生成 → 算 logprob → 算奖励 → 返回 `list[list[Sample]]`。理解了这条默认流水线，你才能理解「在它的哪个工位插自定义逻辑」。

几个对初学者陌生的术语先解释清楚：

- **hook（钩子 / 插入点）**：框架预留的、允许你在固定时机注入自定义代码的位置。slime 的 hook 不是一个事件总线，而是「框架在某处调用一个函数，这个函数的地址由你通过命令行参数指定」。
- **import path（导入路径）**：Python 里定位一个对象的标准写法，形如 `包.子包.模块.函数`。例如 `slime.rollout.sglang_rollout.generate_rollout` 表示 `slime/rollout/sglang_rollout.py` 文件里的 `generate_rollout` 函数。
- **function-path 参数**：slime 所有定制化接口都叫 `--xxx-path`，它的值是一个 import path 字符串。slime 在运行时把这个字符串翻译成真正的函数/类对象，再调用它。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| :--- | :--- |
| [docs/en/get_started/customization.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md) | 官方定制化接口总表，列出全部 21 个 `--xxx-path` 接口、签名与默认实现。本讲的接口表以此为准。 |
| [slime/utils/misc.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py) | 定义 `load_function`，是所有 function-path 接口的「字符串→对象」解析器。 |
| [slime/utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py) | 注册全部 `--xxx-path` 参数并设置默认值（如 `--rollout-function-path`、`--data-source-path`）。 |
| [slime/ray/rollout.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py) | `RolloutManager` 在初始化时集中 `load_function` 一批 rollout 阶段接口。 |
| [slime/rollout/rm_hub/__init__.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py) | `async_rm` 的三级优先级分发，体现 `--custom-rm-path` 如何被调用。 |
| [tests/plugin_contracts/_shared.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py) | 契约测试的共享基础设施（`SLIME_CONTRACT_` 环境变量约定）。 |
| [tests/plugin_contracts/test_plugin_runtime_hook_contracts.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py) | 运行时 hook 契约测试示例，展示「签名匹配 + 调用点稳定」两层断言。 |

## 4. 核心概念与源码讲解

### 4.1 customization 接口表：21+ 个 `--xxx-path` 接口的全景

#### 4.1.1 概念说明

slime 把整个 RL 闭环切成「rollout 数据生成 → 样本转训练数据 → Megatron 训练 → 权重同步」几个阶段，然后在每个阶段的关键位置开一个口子，让你塞进自己的函数。所有口子统一长一个样：一条命令行参数 `--xxx-path`，值是一个 import path 字符串。

为什么要这样设计？因为 RL 任务千差万别（数学题要验答案、工具调用要多轮、沙箱要执行代码），如果把这些逻辑硬编码进框架，框架会臃肿且每换一类任务就 fork 一份。slime 的取舍是：**把「骨架」（采样循环、训练步骤、权重搬运）写死，把「肉」（每个样本怎么生成、怎么打分、怎么过滤）做成可替换的函数**。你只要写一个符合签名的函数，再用 `--xxx-path` 告诉 slime 它在哪，框架就会在对应时机调用它。

这套设计的直接好处是：自定义代码与框架核心**完全解耦**——你的函数在自己的文件里，不被打包进 slime，升级 slime 时你的代码不受影响。

#### 4.1.2 核心流程

先从「四个主接口」的层级关系入手，这是理解全表的钥匙。把它们想象成一座工厂：

```
┌─────────────────────────────────────────────────────────────┐
│  --data-source-path     原料仓库：决定从哪取 prompt          │
│         │                                                    │
│         ▼                                                    │
│  --rollout-function-path  整条流水线（总指挥）                │
│  默认: sglang_rollout.generate_rollout                       │
│      ├── --custom-generate-function-path  单件生成工位       │
│      │     （只替换"一个样本怎么生成"，保留流水线其余部分）   │
│      └── --custom-rm-path  质检打分工位                       │
│            （只替换"一个/一组样本怎么算分"）                  │
└─────────────────────────────────────────────────────────────┘
```

层级的核心含义是：**外层接口包裹内层接口**。

- `--rollout-function-path` 是最外层。它替换的是「整条采样流水线」。默认流水线是 `generate_rollout`，它内部会自动去调 `custom_generate`（生成工位）和 `async_rm`（打分工位）。
- 如果你只设 `--custom-generate-function-path`（生成工位）或 `--custom-rm-path`（打分工位），**整条流水线仍是默认的**，slime 只是在流水线的固定工位上换了人。
- 但如果你替换了 `--rollout-function-path`，新流水线**是你自己写的**，那么「在哪调 custom_generate、在哪调 rm」就由你的代码决定——默认的挂载点不再自动存在。

这条规则解释了 customization 文档里那句反复出现的话：**「大多数 agentic 场景，先用 `--custom-generate-function-path` 加 `--custom-rm-path`，只有在默认 rollout 循环不够用时才替换 `--rollout-function-path`。」** 因为前者改动小、复用多，后者是推倒重来。

除了四个主接口，还有一组「过滤 / 钩子」接口，它们都作用于已经生成出来的 Sample，区别在于**过滤的对象**和**触发时机**：

| 接口 | 操作对象 | 触发时机 | 动作 |
| :--- | :--- | :--- | :--- |
| `--dynamic-sampling-filter-path` | 一组同 prompt 的样本 | 动态采样期间（采样时） | 决定整组要不要丢（DAPO 风格） |
| `--buffer-filter-path` | 缓冲区里的样本组 | 训练前从 buffer 取数时 | 决定 buffer 里取哪些组 |
| `--rollout-sample-filter-path` | 单个 Sample | rollout 结束、转训练数据前 | 给样本打 `remove_sample` 标记 |
| `--rollout-sample-hook-path` | 单个 Sample | 每个样本生成后、算奖励前 | 可改写/替换 Sample 本身 |
| `--rollout-all-samples-process-path` | 全部样本（含被过滤的） | rollout 全部完成后 | 统计/分析，不改训练集 |

这几个很容易混淆，记忆方法是按「**时机从早到晚**」排：动态过滤（采样中）→ 样本钩子（生成完即改）→ 样本过滤（标 remove）→ buffer 过滤（取数时）→ 全量后处理（最后统计）。

#### 4.1.3 源码精读

**接口总表就在 customization 文档里**，这是 slime 唯一一份权威清单：

[docs/en/get_started/customization.md:9-30](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L9-L30) —— 这 21 行表格就是全部 `--xxx-path` 接口的索引，每行还附了到下方详细签名的锚点链接。读源码时，这张表是「目录」。

**几个有默认实现的接口，默认值在 arguments.py 里注册**。注意区分两种默认：一类是「真正有默认函数」（命令行不填也跑得起来），一类是「默认 None」（不填则该功能关闭，走内置逻辑）。

`--rollout-function-path` 有真默认 `slime.rollout.sglang_rollout.generate_rollout`：

[slime/utils/arguments.py:317-330](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L317-L330) —— 这里还把目标签名直接写进了 help 文本：`def generate_rollout(args, rollout_id, data_source, evaluation=False) -> RolloutFnTrainOutput | RolloutFnEvalOutput`。

`--data-source-path` 有真默认 `slime.rollout.data_source.RolloutDataSourceWithBuffer`：

[slime/utils/arguments.py:627-632](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L627-L632) —— 数据源默认用带缓冲区的实现（u3-l3 详述）。

`--eval-function-path` 的默认是「回退到 rollout-function-path」——参数本身默认 None，但校验阶段会补上：

[slime/utils/arguments.py:769-777](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L769-L777) —— help 文本明说「不设就用 rollout_function_path」。

[slime/utils/arguments.py:1905](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1905) —— 校验阶段真正执行回退：`args.eval_function_path = args.rollout_function_path`。这就是「评估默认与训练同函数」的实现。

**`--buffer-filter-path` 是个容易踩坑的默认**：参数默认 None，但代码里不是「None 就不过滤」，而是「None 就回退到 `pop_first`」：

[slime/rollout/data_source.py:172-175](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L172-L175) —— 注意这里 `pop_first` 是**直接引用**而非 `load_function` 加载，所以 buffer 始终有一个默认的 FIFO 过滤器。这意味着「buffer_filter 永远生效」，它不是开关而是「换哪种过滤策略」。

**`--custom-rm-path` 的三级优先级**最能体现「路径解析后立刻调用」的模式：

[slime/rollout/rm_hub/__init__.py:55-63](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L55-L63) —— `async_rm` 按优先级判断：先看样本自带的 `sample.custom_rm_path`（评估数据集配置，最高），再看全局 `args.custom_rm_path`（命令行），都没有才按 `rm_type` 走内置分发。每一支都是 `load_function(path)` 拿到函数后 `await` 调用。

**`--custom-generate-function-path` 在默认流水线内部的挂载点**：

[slime/rollout/sglang_rollout.py:253](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L253) —— 只有默认的 `generate_rollout` 流水线才会走到这里加载 `custom_generate_func`；这正是「替换 rollout-function-path 后，custom-generate 不再自动挂载」的源码证据。

**`RolloutManager` 初始化时集中加载一批 rollout 阶段接口**：

[slime/ray/rollout.py:444-456](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L444-L456) —— 这里一次性 `load_function` 了 `data_source_path`（类，加载后还要实例化）、`rollout_function_path`、`eval_function_path`，以及两个可选的 `custom_reward_post_process_path`、`custom_convert_samples_to_train_data_path`（都先判 `is not None` 再加载）。可以看到模式高度统一：拿字符串 → `load_function` → 存成实例属性 → 后续按需调用。

> 备注：除了文档表格里的 21 个，源码中还存在几个未列入该总表的 path 接口，例如 `--rollout-sample-hook-path`（[slime/utils/arguments.py:474-483](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L474-L483)，`action="append"` 可重复）、`--custom-advantage-function-path`、`--custom-model-provider-path` 等。它们的加载机制与上述完全一致，本讲以官方 21 个总表为主线，这些「表外」接口会在各自专题讲义（如 u6-l4 优势估计器、u4-l5 模型构建）中详述。

#### 4.1.4 代码实践

**实践目标**：把全部 `--xxx-path` 接口按「rollout 阶段 / 训练阶段」分成两大列，并标注每个接口的默认实现或默认值，整理成一张表。这是本讲规格指定的核心实践，做完这张表你就拥有了 slime 定制化的「速查地图」。

**操作步骤（源码阅读型实践）**：

1. 打开 [docs/en/get_started/customization.md:9-30](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L9-L30)，把 21 行接口逐条抄下。
2. 对每个接口，到 `slime/utils/arguments.py` 里用接口名（如 `--custom-loss-function-path`）搜索其 `add_argument`，确认它的 `default=`。
3. 对默认值是 `None` 的接口，进一步看它在源码里「None 时走什么」——例如 `--buffer-filter-path` 走 `pop_first`、`--eval-function-path` 走 `rollout_function_path`、`--custom-rm-path` 走内置 `rm_type` 分发。
4. 按「主要作用于 rollout 阶段 / 主要作用于训练阶段」把接口归入两列（权重同步、转换/日志可作为附加列）。

**需要观察的现象**：你会注意到 `--rollout-function-path`、`--data-source-path`、`--eval-function-path`（经回退）这三个有「真默认」，命令行完全不填也能跑；其余默认 `None`，不填则该功能关闭或走内置逻辑。

**预期结果**：一张形如下面的表（这里给出骨架，请你补全其余行）：

| 接口 | 阶段 | 默认值 / 默认行为 |
| :--- | :--- | :--- |
| `--rollout-function-path` | rollout | `slime.rollout.sglang_rollout.generate_rollout` |
| `--data-source-path` | rollout | `slime.rollout.data_source.RolloutDataSourceWithBuffer` |
| `--eval-function-path` | rollout(评估) | 回退到 `rollout-function-path` |
| `--custom-generate-function-path` | rollout | `None`（用内置生成） |
| `--custom-rm-path` | rollout | `None`（按 `--rm-type` 内置分发） |
| `--buffer-filter-path` | rollout | `None`→回退 `pop_first` |
| `--dynamic-sampling-filter-path` | rollout | `None`（不过滤） |
| ... | ... | ... |
| `--custom-loss-function-path` | 训练 | `None`（需配 `--loss-type custom_loss`） |
| `--custom-megatron-before-train-step-hook-path` | 训练 | `None`（不执行） |
| ... | ... | ... |

> 待本地验证：表格中标注「回退到 X」的几条，建议你用 `Grep` 在源码里亲自确认回退分支，避免记错。

#### 4.1.5 小练习与答案

**练习 1**：如果一个用户同时设置了 `--rollout-function-path my.rollout_fn` 和 `--custom-generate-function-path my.gen`，他的 `my.gen` 会自动被调用吗？

**答案**：不一定。`--custom-generate-function-path` 的挂载点在**默认** `generate_rollout` 流水线内部（[sglang_rollout.py:253](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L253)）。既然他用 `--rollout-function-path` 替换了整条流水线，新的 `my.rollout_fn` 必须自己显式去 `load_function(args.custom_generate_function_path)` 并调用，否则 `my.gen` 不会被执行。

**练习 2**：`--rollout-sample-filter-path` 和 `--buffer-filter-path` 都叫「filter」，它们过滤的对象有何不同？

**答案**：`--buffer-filter-path` 过滤的是**缓冲区里的样本组**（`list[list[Sample]]`），在「训练前从 buffer 取数」时触发，决定取哪些组（[data_source.py:172-175](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L172-L175)）；`--rollout-sample-filter-path` 过滤的是**单个样本是否参与 loss**，在 rollout 结束后触发，通过给 `Sample` 打 `remove_sample` 标记实现（不真正删除，只是不进 loss）。一个管「组级别取数」，一个管「样本级别参与训练」。

**练习 3**：为什么说 `--buffer-filter-path` 永远生效？

**答案**：因为它的默认不是「关闭」，而是回退到内置的 `pop_first`（FIFO）。即使命令行不填，`RolloutDataSourceWithBuffer` 也会 `self.buffer_filter = pop_first`，buffer 取数始终经过一个过滤器——它不是开关，而是「用哪种过滤策略」。

---

### 4.2 `load_function`：import path 字符串如何变成可调用对象

#### 4.2.1 概念说明

所有 `--xxx-path` 接口收到的值都是一个**字符串**，比如 `"slime.rollout.sglang_rollout.generate_rollout"`。但 slime 显然不能「调用一个字符串」，必须先把它变成真正的函数/类对象。这个「字符串→对象」的翻译工作，统一由 `slime/utils/misc.py` 里的 `load_function` 完成。

理解它你需要先回忆 Python 的导入机制：`import a.b.c` 会执行 `a/b/c.py`，并把模块对象挂在 `a.b.c` 这个名字下；而模块里的函数 `f` 就是这个模块对象的一个属性。所以「定位 `a.b.c.f`」可以拆成两步：先导入模块 `a.b.c`，再从模块对象上 `getattr` 取出属性 `f`。`load_function` 就是把这两步打包。

#### 4.2.2 核心流程

`load_function("module.sub.attr")` 的执行流程：

```
输入字符串: "slime.rollout.sglang_rollout.generate_rollout"
   │
   ▼  path.rpartition(".")  从最后一个 "." 切开
module_path = "slime.rollout.sglang_rollout"     # 模块部分
attr        = "generate_rollout"                 # 属性部分
   │
   ▼  importlib.import_module(module_path)
module = <已导入的模块对象 slime.rollout.sglang_rollout>
   │
   ▼  getattr(module, attr)
return <函数对象 generate_rollout>
```

两个关键细节：

1. **从最后一个点切开**（`rpartition(".")`）。「最后一个点」之前的全是模块路径，之后的是属性名。这保证像 `generate_rollout` 这样不含点的名字被当作属性，而不会误当成子模块。
2. **带缓存**（`@cache`）。同一个 import path 在一次运行里只会被解析一次，之后直接返回缓存的对象。这对性能有意义——例如 `async_rm` 每算一个样本的奖励都可能触发 `load_function(args.custom_rm_path)`，若每次都重新 import 会很慢。

一个推论：`load_function` 返回的不一定是函数，也可能是**类**。比如 `--data-source-path` 的值 `slime.rollout.data_source.RolloutDataSourceWithBuffer` 是个类，`load_function` 拿到类对象后，调用方还会再 `cls(args)` 实例化它（见 [rollout.py:444-445](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L444-L445)）。

#### 4.2.3 源码精读

整个函数只有 4 行有效代码，却支撑了全框架二十多个接口：

[slime/utils/misc.py:38-47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py#L38-L47) —— `@cache` 装饰器做记忆化；`path.rpartition(".")` 在最后一个点处切成 `(module_path, ".", attr)`；`importlib.import_module(module_path)` 导入模块；`getattr(module, attr)` 取出属性返回。函数上方的文档字符串给出了典型用法 `"module.submodule.function"`。

逐点说明：

- `@cache`（即 `functools.cache`）让 `load_function` 对相同 `path` 只计算一次。注意它缓存的是「解析结果」，不是「调用结果」——每次仍会真正调用返回的函数。
- `rpartition(".")` 返回三元组 `(head, sep, tail)`。当字符串里没有点时（比如直接传 `"generate_rollout"`），`module_path` 为空、`attr` 为整串，`importlib.import_module("")` 会报错——所以 import path 必须是「带模块路径的完整写法」。
- `importlib.import_module` 与 `import` 语句等价，但它接受字符串参数，这正是「运行时才知道导入谁」的场景所需。
- `getattr(module, attr)` 取属性；若模块里没有这个名字会抛 `AttributeError`，这是最常见的「路径写错」报错。

#### 4.2.4 代码实践

**实践目标**：亲手用 `load_function` 把一个 import path 字符串解析成对象，验证它能被正常调用，从而建立对「字符串→对象」机制的直观感受。

**操作步骤**：

1. 确保已按 u1-l3 安装 slime（`pip install -e . --no-deps`，`import slime` 不报错）。
2. 在仓库根目录启动 Python，执行下面这段**示例代码**（非项目原有代码）：

   ```python
   # 示例代码：手动体验 load_function
   from slime.utils.misc import load_function

   # 把字符串解析成默认 rollout 函数对象
   fn = load_function("slime.rollout.sglang_rollout.generate_rollout")
   print("解析得到:", fn)             # 应打印 <function generate_rollout ...>
   print("是同一个对象:", fn.__module__, fn.__name__)

   # 验证 @cache：再解析一次，比较 id
   fn2 = load_function("slime.rollout.sglang_rollout.generate_rollout")
   print("缓存命中(同一对象):", fn is fn2)   # 应为 True

   # 也可以解析类
   cls = load_function("slime.rollout.data_source.RolloutDataSourceWithBuffer")
   print("解析得到类:", cls)
   ```

3. 故意写一个错误的路径，观察报错：

   ```python
   load_function("slime.rollout.sglang_rollout.not_exist")   # AttributeError
   load_function("not_a_module.anything")                     # ModuleNotFoundError
   ```

**需要观察的现象**：第一次与第二次解析同一路径得到 `fn is fn2` 为 `True`（缓存生效）；错误路径分别抛 `AttributeError`（模块在但属性不在）和 `ModuleNotFoundError`（模块本身找不到）。

**预期结果**：你会切身理解「命令行里写的 `--rollout-function-path slime.rollout.sglang_rollout.generate_rollout`，本质上就是被这 4 行代码翻译成那个函数对象」。

> 待本地验证：如果你尚未安装 slime 的纯 Python 依赖（如 `pybase64`、`torch`），`from slime.utils.misc import load_function` 可能因 `misc.py` 顶部的 `import torch` 而失败。此时可把 `load_function` 的 4 行核心逻辑单独复制到一个脚本里运行，效果一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load_function` 用 `rpartition(".")`（从右边切）而不是 `partition(".")`（从左边切）？

**答案**：因为属性名一定在最后，而模块路径可能含多个点。`rpartition` 在「最后一个点」切开，能把 `a.b.c.f` 正确分成模块 `a.b.c` 和属性 `f`。若用 `partition`（第一个点），会把 `a` 当模块、`b.c.f` 当属性，`getattr` 就找不到了。

**练习 2**：`load_function` 上的 `@cache` 缓存的是「函数对象」还是「函数调用的返回值」？

**答案**：缓存的是**解析结果（函数对象本身）**，不是调用结果。也就是说 `load_function(p)` 第二次直接返回同一个函数对象，但你仍然可以多次调用这个函数、每次都真正执行。这层缓存只省掉了「重复 import 与 getattr」的开销。

**练习 3**：`load_function` 既能返回函数也能返回类。调用方如何区分对待？

**答案**：调用方按接口契约决定。例如 `--data-source-path` 约定给的是类，所以 [rollout.py:444-445](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L444-L445) 拿到后还 `data_source_cls(args)` 实例化；而 `--rollout-function-path` 约定给的是函数，直接当函数调用。`load_function` 本身不区分，区分由每个接口的契约（签名）负责。

---

### 4.3 `plugin_contracts`：用 CPU 契约测试自检自定义实现

#### 4.3.1 概念说明

`load_function` 把字符串变成对象后，slime 并不检查这个对象「长得对不对」——签名错了、返回结构错了，往往要等到真正跑训练（烧 GPU）时才在深层报一个难懂的错。为了让自定义实现「早暴露问题」，slime 提供了一组**纯 CPU 的契约测试**（contract tests）。

「契约测试」的核心思想是：slime 对每个 hook 都有一份**隐式契约**——你给我的函数必须长这个签名、返回这种结构、产生这种副作用。契约测试把这份隐式契约写成可执行的断言，让你在不用 GPU 的情况下验证「我的自定义实现是否符合契约」。它的另一个巧妙之处在于：测试本身也是通过 import path 加载被测对象的（和训练时同一条 `load_function` 路径），所以它能验证**用户通过 CLI 传入的任意实现**，而不只是 slime 内置的实现。

#### 4.3.2 核心流程

契约测试的运行流程：

```
你写了一个自定义实现 my_proj.my_rollout.generate_rollout
   │
   ▼  方式一：设环境变量
export SLIME_CONTRACT_ROLLOUT_FUNCTION_PATH=my_proj.my_rollout.generate_rollout
   │           或 方式二：命令行直传
   │  python tests/plugin_contracts/test_plugin_rollout_contracts.py \
   │      --rollout-function-path my_proj.my_rollout.generate_rollout
   ▼
_shared.run_contract_test_for_file() 把命令行参数转成 SLIME_CONTRACT_XXX 环境变量
   │
   ▼  测试内部 get_contract_path() 读环境变量，没有就用默认 path
path = get_contract_path("ROLLOUT_FUNCTION_PATH", default="slime...generate_rollout")
   │
   ▼  load_function(path) 加载被测对象，跑两层断言：
1. inspect.signature(fn) 的参数与默认实现的参数逐一比对
2. 实际调用 fn，断言返回类型 / 副作用符合契约
```

两层断言的含义：

- **第一层：签名匹配**——你的函数参数名、顺序、个数必须和默认实现一致。这用 `inspect.signature` 比对，是最便宜的检查。
- **第二层：行为匹配**——用一个最小合法输入实际调用你的函数，断言返回类型（如 `RolloutFnEvalOutput`）、关键字段、副作用（如给 `Sample.remove_sample` 打标）正确。

此外，运行时 hook 契约测试还有一层更特别的检查——**调用点稳定**：它会读 slime 源码文件，断言「这个 hook 确实还在源码的这个位置被调用」，防止框架重构后某个 hook 悄悄失效而无人知晓。

#### 4.3.3 源码精读

**共享基础设施在 `_shared.py`**，定义了环境变量约定与运行器：

[tests/plugin_contracts/_shared.py:12](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py#L12) —— 环境变量前缀固定为 `SLIME_CONTRACT_`，所以 `--custom-rm-path` 对应的环境变量就是 `SLIME_CONTRACT_CUSTOM_RM_PATH`（把参数名的连字符转下划线再大写）。

[tests/plugin_contracts/_shared.py:53-54](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py#L53-L54) —— `get_contract_path(key, default)`：优先读 `SLIME_CONTRACT_<key>` 环境变量，读不到就用内置默认 path。这让同一个测试既能验内置实现（不设环境变量），也能验用户实现（设环境变量）。

[tests/plugin_contracts/_shared.py:57-88](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py#L57-L88) —— `run_contract_test_for_file`：把命令行传入的 `--xxx-path` 参数自动转成对应环境变量，再调 `pytest.main` 跑指定测试文件。这就是 customization 文档里「`python tests/plugin_contracts/<file>.py --rollout-function-path ...`」的实现。

**四个测试文件按 hook 形状分组**（见 [customization.md:482-493](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L482-L493)）：

| 测试文件 | 覆盖的接口 |
| :--- | :--- |
| `test_plugin_rollout_contracts.py` | `--rollout-function-path` |
| `test_plugin_generate_contracts.py` | `--custom-generate-function-path` |
| `test_plugin_path_loading_contracts.py` | `--eval-function-path`、`--custom-rm-path`、`--dynamic-sampling-filter-path`、`--buffer-filter-path`、`--data-source-path`、`--rollout-sample-filter-path`、`--rollout-all-samples-process-path` |
| `test_plugin_runtime_hook_contracts.py` | `--custom-rollout-log-function-path`、`--custom-eval-rollout-log-function-path`、`--custom-reward-post-process-path`、`--custom-convert-samples-to-train-data-path`、`--rollout-data-postprocess-path` |

**运行时 hook 测试展示了「签名 + 调用点稳定」双断言」**：

[tests/plugin_contracts/test_plugin_runtime_hook_contracts.py:131-177](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py#L131-L177) —— `HOOK_CASES` 是一张表，每行声明一个 hook：`env_key`（环境变量名）、`default_path`（默认实现）、`source_path`（slime 里调用它的源码文件）、`runtime_marker`（调用点的代码片段）、`expected_params`（期望的参数元组）。这种「数据驱动」写法让新增一个 hook 只需加一行表。

[tests/plugin_contracts/test_plugin_runtime_hook_contracts.py:180-182](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py#L180-L182) —— `test_runtime_hook_callsite_is_stable`：直接读 `source_path` 文件文本，断言 `runtime_marker` 片段仍在。这保证「框架确实还在这个地方调用这个 hook」。

[tests/plugin_contracts/test_plugin_runtime_hook_contracts.py:185-189](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py#L185-L189) —— `test_runtime_hook_path_aligns_with_expected_format`：`load_function` 加载被测对象（经 `get_contract_path` 取 path），断言其 `inspect.signature` 的参数元组等于 `expected_params`，再 `case.invoke(fn)` 实际调用验证行为。

#### 4.3.4 代码实践

**实践目标**：运行一个现成的契约测试，体会「它如何用一个最小输入验证 hook 的签名与返回结构」，为以后给你自己的自定义写自检打基础。

**操作步骤**：

1. 进入仓库根目录，运行「路径加载」契约测试文件（它可直接 `python` 执行，兼容 `run-ci-changed`）：

   ```bash
   python tests/plugin_contracts/test_plugin_path_loading_contracts.py
   ```

2. 观察它验证的内置默认实现。比如它会断言默认 buffer_filter（`pop_first`）签名前 4 个参数是 `("args", "rollout_id", "buffer", "num_samples")`，见 [test_plugin_path_loading_contracts.py:224-226](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_path_loading_contracts.py#L224-L226)。

3. 再跑全部四个契约测试文件（即文档给出的完整命令）：

   ```bash
   python -m pytest \
     tests/plugin_contracts/test_plugin_rollout_contracts.py \
     tests/plugin_contracts/test_plugin_generate_contracts.py \
     tests/plugin_contracts/test_plugin_path_loading_contracts.py \
     tests/plugin_contracts/test_plugin_runtime_hook_contracts.py
   ```

4. （进阶）挑一个内置 hook，用命令行覆盖方式指向它自己，验证「覆盖机制」生效：

   ```bash
   python tests/plugin_contracts/test_plugin_path_loading_contracts.py \
     --buffer-filter-path slime.rollout.data_source.pop_first
   ```

**需要观察的现象**：第 1、3 步应全部通过（绿色的 `.` 或 `passed`）；第 4 步因为指向的就是默认实现，也应通过。若你故意把 path 写错（如 `...pop_firs`，少个 `t`），应看到 `ModuleNotFoundError`/`AttributeError`。

**预期结果**：你建立了「契约测试 = 用 `load_function` 加载被测对象 + 比签名 + 跑最小调用 + 验返回」的完整心智，且知道这套测试**不需要 GPU**。

> 待本地验证：步骤 1、3 的具体通过数量取决于当时仓库的测试集大小；若环境缺纯 Python 依赖（如 `pytest`、`torch`），需先 `pip install -r requirements.txt`。运行时 hook 测试会真实读取 slime 源码文件文本，因此必须在仓库内执行。

#### 4.3.5 小练习与答案

**练习 1**：为什么契约测试要用 `load_function`（即和训练时同一条字符串→对象路径）来加载被测对象，而不是直接 `from my_proj import my_rollout`？

**答案**：因为训练时 slime 就是按 import path 字符串经 `load_function` 加载你的实现的。契约测试走同一条路径，才能真实复现「训练时到底会加载到哪个对象」，从而覆盖 CLI 参数拼写、环境变量、路径解析等整条链路，而不只是验证 Python 语义层面「这个函数存在」。

**练习 2**：`test_runtime_hook_callsite_is_stable` 为什么要去读 slime 源码文件、断言一段调用代码还在？

**答案**：这是为了防止**框架重构导致 hook 静默失效**。如果一个 hook 的调用点在重构中被删掉，那么即使你的自定义函数签名完全正确，它也永远不会被调用——这种 bug 极难发现。该测试把「调用点代码片段」当作锚点，一旦片段消失就立刻失败，相当于给每个 hook 买了一份「调用点仍在」的保险。

**练习 3**：我要自检自己的 `my_rollout.generate_rollout`，除了命令行 `--rollout-function-path`，还能怎么做？

**答案**：设环境变量 `SLIME_CONTRACT_ROLLOUT_FUNCTION_PATH=my_rollout.generate_rollout` 后再跑 `test_plugin_rollout_contracts.py`。命令行方式和环境变量方式最终都被 `_shared.run_contract_test_for_file` / `get_contract_path` 归一成「读 `SLIME_CONTRACT_*` 环境变量」，二者等价（见 [_shared.py:57-88](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py#L57-L88)）。

## 5. 综合实践

**任务**：为下面这个场景选择正确的定制化接口组合，并写一份「接口选择说明书」。

> 场景：你要训练一个「带沙箱执行的代码生成 agent」。每个 prompt 需要：① 让模型生成一段代码；② 在沙箱里运行它，把运行输出/报错拼回上下文，模型再生成修正——可能多轮；③ 沙箱运行成功与否决定奖励（0/1）；④ 你希望复用 slime 默认的过采样、abort、buffer 管理等流水线能力，不想自己重写。

请完成：

1. **选择接口**：从本讲的四个主接口里挑出你需要的（提示：参考 [customization.md:38-47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L38-L47) 的决策表）。说明为什么选它、为什么不选 `--rollout-function-path`。
2. **标注 loss_mask**：在「模型生成的 token」与「沙箱输出拼回的 token」上，`loss_mask` 分别该设什么？为什么？（提示：只有模型自己生成的 token 才该参与策略梯度。）
3. **若一整条轨迹被拆成多段**：你需要让多段共享同一个 `rollout_id`，这背后的契约是什么？（提示：见 [customization.md:87-91](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L87-L91)。）
4. **自检**：写出你要用的契约测试文件名与对应的环境变量名（参考 4.3.3 的分组表）。

**参考思路**（先自己想再对照）：

1. 选 `--custom-generate-function-path`（实现多轮「生成→沙箱→拼回」循环）+ `--custom-rm-path`（沙箱成功判 0/1）。不选 `--rollout-function-path`，因为默认流水线的过采样/abort/buffer 仍可复用，只需替换「单样本生成」和「打分」两个工位——这正是 4.1.2 强调的「先用工位接口、最后才换流水线」。
2. 模型生成 token 的 `loss_mask=1`（参与训练），沙箱输出 token 的 `loss_mask=0`（环境观测，不该让模型为它负责）。
3. 契约：同一 rollout 产出的兄弟段必须共享同一 `rollout_id`，slime 才会把它们当作「同一次 rollout 的分段」聚合，而不是当成多次独立 rollout 重复计数。
4. `custom-generate` 用 `test_plugin_generate_contracts.py` + `SLIME_CONTRACT_CUSTOM_GENERATE_FUNCTION_PATH`；`custom-rm` 用 `test_plugin_path_loading_contracts.py` + `SLIME_CONTRACT_CUSTOM_RM_PATH`。

> 说明：本综合实践是「设计 + 论证」型任务，无需 GPU。完成它意味着你已能针对真实任务在 21+ 个接口里做出正确取舍——这正是本讲的最终目标。具体 custom-generate 的代码写法见 u6-l2，custom-rm 见 u6-l3。

## 6. 本讲小结

- slime 用一套统一的 `--xxx-path` 参数（21+ 个）暴露全部定制点，值是 import path 字符串，由 `load_function` 在运行时解析成函数/类对象再调用——自定义代码与框架核心完全解耦。
- 四个主接口有明确层级：`--data-source-path`（原料）→ `--rollout-function-path`（整条流水线，包裹内层）→ `--custom-generate-function-path`（生成工位）+ `--custom-rm-path`（打分工位）。换外层会让内层挂载点失效，所以应优先用工位接口。
- 一组易混的「过滤/钩子」接口按时机区分：动态过滤（采样中）→ 样本钩子（生成后即改）→ 样本过滤（标 remove）→ buffer 过滤（取数时，默认 `pop_first` 始终生效）→ 全量后处理（最后统计）。
- `load_function` 只有 4 行核心逻辑（`rpartition` 切最后一点 → `import_module` → `getattr`），加 `@cache` 记忆化；它能返回函数也能返回类，区分由各接口契约负责。
- `tests/plugin_contracts/` 提供纯 CPU 契约测试，按 hook 形状分四个文件，用「签名匹配 + 最小调用验返回 + 调用点稳定」三层断言，经 `SLIME_CONTRACT_*` 环境变量或命令行覆盖即可验用户自定义实现，无需 GPU。

## 7. 下一步学习建议

本讲只建立了全景与加载机制，每个接口的具体写法在后续讲义展开：

- **u6-l2 自定义生成函数（custom-generate）**：以 search-r1 多轮检索为例，手把手写一个 `async def generate(args, sample, sampling_params)`，重点讲多轮交互的 loss_mask 标注与 fan-out。
- **u6-l3 自定义奖励与样本→训练数据转换**：讲 `--custom-rm-path`（含 batch group_rm）与 `--custom-convert-samples-to-train-data-path`，打通奖励到训练输入的最后一步。
- **u6-l4 优势估计器与 RL 算法选择**：精读 `ppo_utils.py` 的 GRPO/GSPO/RLOO/ReMax/PPO 等估计器，这是理解 `--custom-reward-post-process-path`、`--custom-advantage-function-path` 作用域的前提。
- **u6-l5 自定义损失、TIS 与 off-policy 修正**：讲 `--custom-loss-function-path`、`--custom-tis-function-path` 与 OPSM/CISPO，处理异步/过期样本的 off-policy 问题。

建议读者在进入 u6-l2 前，先把本讲 4.1.4 的接口表实践做完——那张表是你后续阅读所有定制化讲义的速查地图。
