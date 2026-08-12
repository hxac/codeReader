# Workflow 编排骨架与运行模式

## 1. 本讲目标

上一讲（u3-l1）我们跟到了「`parser_gen` 解析参数 → 构造 Workflow → `run()`」的门口，但 Workflow 内部长什么样、`run()` 到底怎么执行，被刻意留到了本讲。

本讲带你走进 `amct_pytorch/workflows/` 目录，剖析四条命令背后的四个 Workflow 类：

- `LlmPtqWorkflow`（ptq，逐层优化量化参数）
- `LlmEvalWorkflow`（eval，评估 PPL）
- `LlmDeployWorkflow`（deploy，烘焙导出可部署权重）
- `LlmExtractPtqDataWorkflow`（extract_ptq_data，录制校准数据）

学完后你应当能够：

1. 说出四个 Workflow 共用的「`setup()` → 按 granularity 分发 → 移除日志 sink」控制流骨架。
2. 解释 `--granularity` 如何决定走 `_run_blockwise` 还是其它路径，以及它在 PTQ 里为什么还兼做 solver 选型。
3. 理解 `_register_components()` 的「惰性注册 + 幂等保护」机制，以及为什么四个 Workflow 注册的组件集合不一样。

> 本讲只讲**编排骨架**，不展开 Workflow 内部调用的「逐层前向 / PTQ 单元划分 / 重建优化 / 部署导出」等具体业务逻辑——它们分别属于 u4、u5 系列讲义。

---

## 2. 前置知识

### 2.1 什么是 Workflow（编排器）

你可以把 Workflow 理解成一个**施工队长**：它自己不动手做量化，而是负责排好顺序、准备好材料（模型、数据、日志）、再把活分派给具体的工人（pipeline、solver、data provider）。

AMCT 把每条 CLI 命令（eval / extract_ptq_data / ptq / deploy）实现成一个独立的 Workflow 类，这样四条命令的**控制流高度一致**，但**各自只装自己需要的零件**。

### 2.2 granularity（运行粒度）这个词的多义

在 AMCT 里 `granularity` 是个**被重载的词**，初学很容易混淆：

| 出现位置 | 取值 | 含义 |
|---|---|---|
| Workflow 层的 `--granularity` 参数（本讲） | `block` / `model` / `tensor` | **运行粒度**：决定 Workflow 走逐层流水线还是整模型一把过 |
| classic 经典量化算子的 `weights.strategy` 等 | `tensor` / `channel` / `group` | **量化粒度**：一个 scale 管多少个元素（见 u2-l1） |

本讲只讲**第一种**（Workflow 运行粒度）。它回答的问题是：「处理这个模型时，是一层一层来（block），还是整个模型一次过（model），还是按张量逐个转换（tensor）？」

### 2.3 loguru 的 sink 概念

AMCT 用 `loguru` 做日志。`logger.add(文件路径, ...)` 会给全局 logger **挂一个输出目的地（sink）**，返回一个 `sink_id`。`run()` 结束时调用 `logger.remove(sink_id)` 把这个文件 sink 卸掉，保证每条命令的日志只写进自己那个文件、不互相串。理解这一点，才能看懂下面四个 `run()` 末尾那句 `logger.remove(sink_id)`。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [amct_pytorch/workflows/llm_ptq.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py) | PTQ 主流程 Workflow，本讲的主角，含最完整的 setup/run 与断点续跑逻辑 |
| [amct_pytorch/workflows/llm_eval.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_eval.py) | 评估 Workflow，演示 block / model 双路径都可用 |
| [amct_pytorch/workflows/llm_deploy.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py) | 部署导出 Workflow，演示 block / tensor 双路径 |
| [amct_pytorch/workflows/llm_extract_ptq_data.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_extract_ptq_data.py) | 校准数据录制 Workflow，只支持 block |
| [amct_pytorch/cli/llm/args.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py) | `--granularity` 参数定义（默认值 `model`） |
| [amct_pytorch/common/optimization/\_\_init\_\_.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/__init__.py) | `register_solvers()` 与 `_REGISTERED` 幂等保护 |
| [amct_pytorch/common/utils/run_logging.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/run_logging.py) | `setup_run_logging()`：挂文件 sink 的实现 |

---

## 4. 核心概念与源码讲解

### 4.1 四个 Workflow 的统一骨架：setup → run

#### 4.1.1 概念说明

虽然四个 Workflow 干的活完全不同（评测、录数据、训练、导出），但它们的**外层壳子长得几乎一样**。这是一套刻意设计的编排骨架：

```
Workflow(args)
   │
   └── run()                          # 唯一公开入口（CLI 的 main() 调它）
          │
          ├── setup()                 # 1) 准备阶段：注册组件 → 建 pipeline → 挂日志 sink
          │       ├── _register_components()
          │       ├── _build_pipeline()
          │       └── setup_run_logging()  → 返回 sink_id
          │
          ├── 按 granularity 分发      # 2) 执行阶段：走 _run_blockwise / _run_modelwise / _run_tensorwise
          │
          └── logger.remove(sink_id)   # 3) 收尾：卸掉本次的文件 sink
```

为什么要统一成这个形状？因为四条命令在「准备 → 执行 → 收尾」上的需求是同构的，差别只在「执行」那一步内部装什么。把骨架抽到一致，既方便维护（改一处日志/注册逻辑，四处生效），也让读者只要看懂一个 Workflow，就能举一反三。

#### 4.1.2 核心流程

以最完整的 PTQ Workflow 为例，它的 `setup()` 和 `run()` 配合如下：

1. **`run()` 先调 `setup()`**，拿到一个 `sink_id`（本次运行专属的日志文件句柄）。
2. **`setup()` 内部按固定顺序准备**：注册组件 → 准备实验目录 → 构建 pipeline → 构建 data provider → 挂日志 sink。
3. **回到 `run()`，按 granularity 选执行分支**，并把结果存进 `results`。
4. **收尾 `logger.remove(sink_id)`**，卸掉日志 sink，返回 `results`。

#### 4.1.3 源码精读

先看 PTQ 的 `setup()`——它把四个准备步骤排得清清楚楚：

注册组件 → 准备目录 → 建 pipeline → 建 data provider → 挂日志，最后返回 `sink_id`：
[amct_pytorch/workflows/llm_ptq.py:69-78](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L69-L78)

```python
def setup(self):
    self._register_components()
    self._prepare_experiment_dirs()
    self.pipeline = self._build_pipeline()
    self.data_provider = self._build_data_provider()
    log_name = "ptq"
    if self.granularity == "block":
        log_name = f"ptq_{self.args.start_block_idx}_{self.args.end_block_idx}"
    sink_id, _ = setup_run_logging(self.args, log_name)
    return sink_id
```

> 注意一个小细节：PTQ 的日志文件名会随 `granularity` 和 block 区间变化（`ptq_0_31.log` 这种），方便你给每一层段单独留日志。

再看 `run()`——它把 `setup()` 的产物（`sink_id`）和 granularity 分发、收尾串起来：
[amct_pytorch/workflows/llm_ptq.py:80-91](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L80-L91)

```python
def run(self):
    sink_id = self.setup()
    solver_cls = SOLVER_REGISTRY.get(self.granularity)
    if self.granularity == "block":
        results = self._run_blockwise(solver_cls)
    elif self.granularity == "model":
        results = self._run_modelwise(solver_cls)
    else:
        raise ValueError(f"Unsupported solver granularity '{self.granularity}'.")
    logger.remove(sink_id)
    return results
```

这个「`run()` 调 `setup()` → 分发 → `logger.remove(sink_id)`」的三段式，在另外三个 Workflow 里几乎逐行复刻。对比 eval 的 `run()`：
[amct_pytorch/workflows/llm_eval.py:68-80](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_eval.py#L68-L80) ——同样是 `sink_id = self.setup()` → 按 granularity 分发 → `logger.remove(sink_id)`，只是分发后多打了一条 PPL 汇总日志。

而 `setup_run_logging` 本身就是给 loguru 挂一个文件 sink，返回它的 id：
[amct_pytorch/common/utils/run_logging.py:30-41](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/run_logging.py#L30-L41)

```python
def setup_run_logging(args, task_name: str):
    log_dir = ensure_log_dir(args)
    log_path = os.path.join(log_dir, f"{task_name}.log")
    sink_id = logger.add(log_path, level="INFO", encoding="utf-8", ...)
    return sink_id, log_path
```

正因为 sink 是**本次运行临时挂上去**的，`run()` 末尾必须 `logger.remove(sink_id)` 把它卸掉，否则下一次命令会把日志继续往这个文件里写。

#### 4.1.4 代码实践

**实践目标**：亲手验证四个 Workflow 的 `run()` 都遵守同一套三段式骨架。

**操作步骤**：

1. 打开本讲源码地图列出的四个 `run()` 方法，对照下表逐行核对：

   | Workflow | `sink_id = self.setup()` 所在行 | 分发关键字 | `logger.remove(sink_id)` 所在行 |
   |---|---|---|---|
   | `LlmPtqWorkflow` | llm_ptq.py:81 | `block` / `model` | llm_ptq.py:90 |
   | `LlmEvalWorkflow` | llm_eval.py:69 | `block` / `model` | llm_eval.py:80 |
   | `LlmDeployWorkflow` | llm_deploy.py:88 | `block` / `tensor` | llm_deploy.py:97 |
   | `LlmExtractPtqDataWorkflow` | llm_extract_ptq_data.py:57 | 仅 `block` | llm_extract_ptq_data.py:64 |

2. 找一找：四个 `run()` 里，哪一个在 `logger.remove` 之前多做了一件事（提示：它打了一条 PPL 汇总）。

**需要观察的现象**：四个 `run()` 的首尾两行（`setup()` 与 `logger.remove`）结构完全一致，差异只在中间的分发分支。

**预期结果**：你会确认 AMCT 的四条命令共享同一个外壳，骨架的「稳定区」是 setup 与收尾，「变化区」是中间的 granularity 分发。

**待本地验证**：行号可能随版本微调，请以你本地 HEAD 为准重新核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `setup()` 要返回 `sink_id`，而不是把它存成 `self.sink_id`？

> **参考答案**：`sink_id` 是 `run()` 局部生命周期的资源——`run()` 一结束就要立即 `logger.remove` 卸掉。把它作为 `setup()` 的返回值、由 `run()` 局部变量持有，作用域刚好覆盖「挂上 → 用完 → 卸掉」这段区间，语义清晰且不会泄漏成实例属性被遗忘。这是一种「资源获取即返回、调用方负责释放」的写法。

**练习 2**：如果把 `run()` 末尾的 `logger.remove(sink_id)` 删掉，连续跑 eval 和 ptq 两条命令会发生什么？

> **参考答案**：第一次运行挂上的文件 sink 不会被卸掉，于是它一直挂在全局 logger 上；第二条命令运行时，它的日志（甚至所有 INFO）会**继续被写进第一条命令的日志文件**，造成日志串台、文件越来越大。`logger.remove` 就是为了防止这种跨命令污染。

---

### 4.2 granularity 分发逻辑

#### 4.2.1 概念说明

`--granularity` 是 Workflow 层最重要的分流开关。它决定 `run()` 走哪个 `_run_xxx` 方法。但要注意它有**两副面孔**：

1. **作为代码分支选择器**：所有四个 Workflow 都用它来 if/elif 选 `_run_blockwise`、`_run_modelwise` 或 `_run_tensorwise`。
2. **（仅 PTQ）作为 solver 注册表键**：PTQ 还额外用 `self.granularity` 去 `SOLVER_REGISTRY` 里取求解器类——`SOLVER_REGISTRY.get(self.granularity)`。也就是说，`granularity="block"` 不仅选了 `_run_blockwise`，还选了 `BlockwiseSolver`。

这种「一个开关同时决定执行路径和执行器类型」的设计，让 PTQ 的扩展点很整齐：将来若要加一种新粒度的求解器，只要往 `SOLVER_REGISTRY` 注册同名键、并补一个 `_run_xxx` 分支即可。

#### 4.2.2 核心流程

先把四个 Workflow 对 granularity 的支持情况列成一张矩阵（这是本讲最重要的速查表）：

| Workflow | `block` | `model` | `tensor` | 默认值（args.py） |
|---|---|---|---|---|
| `LlmEvalWorkflow` | ✅ `_run_blockwise` | ✅ `_run_modelwise` | ❌ | `model` |
| `LlmExtractPtqDataWorkflow` | ✅ | ❌（抛异常） | ❌ | `model` |
| `LlmPtqWorkflow` | ✅ | ❌（占位，抛 `ValueError`） | ❌ | `model` |
| `LlmDeployWorkflow` | ✅ `_run_blockwise` | ❌ | ✅ `_run_tensorwise` | `model` |

两点要特别注意：

- **参数默认值是 `model`，但几乎所有示例脚本都显式传 `--granularity block`**。这是因为 `model` 是四条命令**共用参数表**的全局默认（见 u3-l1），而 PTQ/extract/deploy 在 `model` 下并未实现，所以必须显式覆盖。这解释了为什么 [examples/ptq_single_npu.sh](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/ptq_single_npu.sh)、[examples/extract_ptq_data.sh](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/extract_ptq_data.sh)、[examples/deploy.sh](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/deploy.sh)、[examples/eval.sh](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/eval.sh) 里每一行都带 `--granularity block`。
- **`tensor` 是 deploy 专属**：它用于「不重训、只做张量级格式转换」的场景，典型例子是把官方 FP8/FP4 权重反量化成 bf16（见 [examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md) 中「设置 `granularity = tensor` 先转 bf16」的说明）。

分发流程可以用一段伪代码概括（以 PTQ 为例）：

```
读 self.granularity
if == "block":  results = _run_blockwise(solver_cls)   # 逐层、可断点续跑
elif == "model": results = _run_modelwise(solver_cls)   # 当前抛 ValueError（占位）
else: raise ValueError
```

#### 4.2.3 源码精读

先看参数定义本身——注意默认值就是 `model`：
[amct_pytorch/cli/llm/args.py:52-57](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L52-L57)

```python
parser.add_argument(
    '--granularity',
    type=str,
    default='model',
    help='eval for block-wise or global.',
)
```

再看 deploy 的 `run()`——它是唯一同时支持 `block` 和 `tensor` 两条路径的 Workflow，最能体现「granularity = 分发开关」这件事：
[amct_pytorch/workflows/llm_deploy.py:87-98](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L87-L98)

```python
def run(self):
    sink_id = self.setup()
    if self.granularity == "block":
        results = self._run_blockwise()
    elif self.granularity == "tensor":
        results = self._run_tensorwise()
    else:
        raise ValueError(f"Unsupported granularity '{self.granularity}' for deploy.")
    logger.remove(sink_id)
    return results
```

对比 extract_ptq_data 的 `run()`——它只认 `block`，其它一律抛错：
[amct_pytorch/workflows/llm_extract_ptq_data.py:56-65](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_extract_ptq_data.py#L56-L65)

```python
def run(self):
    sink_id = self.setup()
    if self.granularity == "block":
        results = self._run_blockwise()
    else:
        raise ValueError(
            f"Currently unsupported granularity '{self.granularity}' for extract ptq data."
        )
    logger.remove(sink_id)
    return results
```

而 PTQ 里 granularity 的「第二副面孔」——兼做 solver 选型键——体现在这一行：
[amct_pytorch/workflows/llm_ptq.py:83](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L83)

```python
solver_cls = SOLVER_REGISTRY.get(self.granularity)
```

对应的注册在优化模块里——`block` 这个名字被绑到了 `BlockwiseSolver`：
[amct_pytorch/common/optimization/blockwise_solver.py:32](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L32) 与 [amct_pytorch/common/optimization/global_solver.py:26](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/global_solver.py#L26)

```python
@SOLVER_REGISTRY.register(name="block", description="Blockwise calibration solver")
class BlockwiseSolver(BaseSolver): ...

@SOLVER_REGISTRY.register(name="global", description="Global calibration solver")
class GlobalSolver(...): ...
```

所以 PTQ 里 `granularity` 和 `SOLVER_REGISTRY` 的键是**同名约定**的：`block` → `BlockwiseSolver`，`global` → `GlobalSolver`。这也解释了为什么 PTQ 的 `_run_modelwise` 即便实现了也没法直接用——它的分支条件是 `granularity == "model"`，而 solver 注册表里并没有名为 `"model"` 的键（只有 `"global"`）。

#### 4.2.4 代码实践

**实践目标**：用「故意传错」的方式，亲眼看 granularity 分发的报错分支。

**操作步骤**：

1. 找一个 extract_ptq_data 的示例脚本（如 `examples/extract_ptq_data.sh`），把它里面的 `--granularity block` 改成 `--granularity model`。
2. 阅读源码预判它会走到 `llm_extract_ptq_data.py:61` 的哪个 `raise ValueError`。
3. （可选）在有模型与 NPU 环境时实际运行一次；若无环境，则做**源码阅读型实践**：在四个 `run()` 里分别标出「传 `model` / `tensor` / 任意其它值」各会命中哪一行。

**需要观察的现象**：不同 Workflow 对同一个非 `block` 值的反应不同——extract 和 ptq 抛 `ValueError`，deploy 的 `tensor` 却能正常跑 `_run_tensorwise`，eval 的 `model` 也能跑 `_run_modelwise`。

**预期结果**：你会直观体会到「granularity 矩阵」不是抽象表格，而是源码里实打实的 if/elif 分支——传错值就会被对应的 `raise ValueError` 拦下。

**待本地验证**：实际运行需要 NPU、CANN 与模型权重；若不具备，以源码阅读结论为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 args.py 把 `--granularity` 默认值设成 `model`，而 PTQ 实际只支持 `block`？

> **参考答案**：`--granularity` 是四条命令**共用**的一份参数（见 u3-l1 的「共用参数表」），默认值要照顾所有命令。`model` 对 eval 是有效且省事的默认（整模型一把过最直接），但对 PTQ/extract/deploy 并未实现。所以默认值取了一个「对某些命令合理」的值，其余命令要求用户显式传 `--granularity block` 覆盖。

**练习 2**：PTQ 里 `solver_cls = SOLVER_REGISTRY.get(self.granularity)` 这一行，如果用户传 `--granularity model`，会发生什么？

> **参考答案**：`SOLVER_REGISTRY` 里只有 `"block"`（BlockwiseSolver）和 `"global"`（GlobalSolver）两个键，并没有 `"model"`。`Registry.get("model")` 会因查无此项而报错（未注册）。即使强行绕过，下一步 `self.granularity == "model"` 命中 `_run_modelwise`，而该方法当前直接 `raise ValueError`（[llm_ptq.py:217-220](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L217-L220)），说明 modelwise 在 PTQ 里只是占位。

---

### 4.3 _register_components 的注册时机

#### 4.3.1 概念说明

AMCT 是**插件化**的：模型适配器、量化算法、数据类型、求解器都是「先注册到一个全局注册表，运行时按名字取用」。关于注册表本身的机制（`Registry` 基类、装饰器、`get/list_all`）属于 u3-l3，本讲只聚焦一个更朴素的问题：

> **这些注册动作到底在什么时候发生？**

答案是：**惰性注册**——注册不发生在 `import amct_pytorch` 时，而是发生在每次 `run()` 一开始的 `setup()` 里，由 `_register_components()` 触发。

这种设计有两个好处：

1. **启动快**：`import` 时不加载一票重模块（如各模型的适配器、算法实现），冷启动延迟低。
2. **按需装配件**：每个 Workflow 只注册自己用得到的组件，互不干扰。

而为了让「每次 `run()` 都调一次注册」不会重复注册、不会报「已存在」错，每个 `register_xxx()` 内部都有一道**幂等保护**：用一个模块级 `_REGISTERED` 布尔位，注册过就直接 return。

#### 4.3.2 核心流程

四个 Workflow 的 `_register_components()` 各自只注册自己需要的子集：

| Workflow | 注册内容 | 为什么需要 / 不需要 |
|---|---|---|
| `LlmPtqWorkflow` | algorithms + models + dtype + **solvers** | 要训练量化参数，必须装求解器 |
| `LlmEvalWorkflow` | models + dtype + algorithms | 只前向评测，不需要 solver |
| `LlmDeployWorkflow` | models + dtype + algorithms | 只烘焙导出，不需要 solver |
| `LlmExtractPtqDataWorkflow` | **只 models** | 只需加载模型做前向录数据，连算法/dtype 都用不到 |

规律很清楚：**越靠后、越「重」的组件，只有真正需要的 Workflow 才装**。extract 最轻（只要模型），ptq 最重（算法+数据类型+求解器全要）。

注册的时序在 `setup()` 里是**第一步**（先于建 pipeline），因为 `_build_pipeline()` 要用 `MODEL_REGISTRY.get(model_name)` 取模型适配器——必须先注册模型，才能取到。

#### 4.3.3 源码精读

先看四个 `_register_components()` 的差异——PTQ 是全集，extract 只有一个：
[amct_pytorch/workflows/llm_ptq.py:52-57](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L52-L57)

```python
@staticmethod
def _register_components():
    register_algorithms()
    register_llm_models()
    register_dtype()
    register_solvers()
```

[amct_pytorch/workflows/llm_extract_ptq_data.py:43-45](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_extract_ptq_data.py#L43-L45)

```python
@staticmethod
def _register_components():
    register_llm_models()
```

eval 与 deploy 介于两者之间（模型 + 数据类型 + 算法，无 solver）：
[amct_pytorch/workflows/llm_eval.py:51-55](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_eval.py#L51-L55) 与 [amct_pytorch/workflows/llm_deploy.py:68-72](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L68-L72)。

再看幂等保护是怎么实现的——以 `register_solvers()` 为例：
[amct_pytorch/common/optimization/\_\_init\_\_.py:24-34](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/__init__.py#L24-L34)

```python
_REGISTERED = False

def register_solvers():
    global _REGISTERED
    if _REGISTERED:
        return
    from .blockwise_solver import BlockwiseSolver  # noqa: F401
    _REGISTERED = True
```

关键点：第一次调用时 `_REGISTERED` 为 `False`，于是 `import BlockwiseSolver`（这个 import 的副作用就是触发类定义上的 `@SOLVER_REGISTRY.register(...)` 装饰器，完成真正的注册），然后把标志位置 `True`。之后再调用（比如你又跑了一次 PTQ、或同一个进程里连跑两条命令），直接 `return`，既不重复 import、也不会触发「重复注册」报错。

而 `_register_components()` 在 `setup()` 里是**第一行**，排在 `_build_pipeline()` 之前：
[amct_pytorch/workflows/llm_ptq.py:69-73](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L69-L73)

```python
def setup(self):
    self._register_components()      # ① 先注册，保证注册表里有模型/算法/solver
    self._prepare_experiment_dirs()
    self.pipeline = self._build_pipeline()   # ② 再 MODEL_REGISTRY.get(model_name)
    self.data_provider = self._build_data_provider()
    ...
```

顺序不能反：`_build_pipeline()` 里是 `MODEL_REGISTRY.get(self.model_name)`（[llm_ptq.py:136-138](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L136-L138)），如果还没注册模型就去取，注册表里就是空的。

#### 4.3.4 代码实践

**实践目标**：验证「注册是惰性的、且幂等」。

**操作步骤**：

1. 打开 Python（装好 amct_pytorch 的环境），执行：

   ```python
   import amct_pytorch                    # 只 import，不触发业务注册
   from amct_pytorch.common.models import MODEL_REGISTRY
   print(MODEL_REGISTRY.list_all())      # 观察此时注册表是否为空/很少
   ```

2. 接着手动触发一次模型注册：

   ```python
   from amct_pytorch.common.models.llm import register_llm_models
   register_llm_models()
   print(MODEL_REGISTRY.list_all())      # 现在应当列出一批已注册模型
   register_llm_models()                 # 再调一次
   print("第二次调用没有报错 = 幂等保护生效")
   ```

**需要观察的现象**：仅 `import` 时注册表内容很少（或为空）；调用 `register_llm_models()` 后立刻出现一批模型名；第二次调用不报「重复注册」错误。

**预期结果**：印证「注册发生在 `register_xxx()` 调用时（即 `setup()` 里），而非 import 时」，且 `_REGISTERED` 幂等位让重复调用安全无副作用。

**待本地验证**：若未安装 amct_pytorch，可改为**源码阅读型实践**——在 `common/models/llm/__init__.py` 中找到 `register_llm_models` 的定义与它内部的 `_REGISTERED` 判断，确认与 `register_solvers` 同构。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_register_components()` 是 `@staticmethod`，而不是用实例方法读 `self.args`？

> **参考答案**：注册动作是**全局副作用**（往模块级注册表里写），与具体某个 Workflow 实例的 `args` 无关——注册的是「有哪些模型/算法可用」，而不是「这次跑哪个模型」。把它做成静态方法，正好表明它不依赖实例状态，纯粹是一次全局初始化。

**练习 2**：假设你要新增一个只在 deploy 用到的「部署专用算法」，应该改哪个 Workflow 的 `_register_components()`？为什么 PTQ 不受影响？

> **参考答案**：只改 `LlmDeployWorkflow._register_components()` 即可（或在该方法里多调一个 `register_deploy_algos()`）。因为每个 Workflow 只注册自己需要的子集，PTQ 的 `_register_components()` 不调这个新函数，就不会把部署专用算法装进 PTQ 的运行路径，互不污染——这正是「按 Workflow 装配件」的好处。

---

## 5. 综合实践

本讲的综合实践，把三个最小模块串成一张图。请完成下述**源码阅读 + 画图**任务（无需运行环境）：

**任务**：对比四个 Workflow 类的 `run()` 方法，画出它们共用的控制流，并定位 PTQ 独有的断点续跑逻辑。

**操作步骤**：

1. **画共用骨架**。在一张图上画出下面这条控制流（四个 Workflow 共用）：

   ```
   run()
     ├─ sink_id = setup()
     │     ├─ _register_components()   # 4.3：惰性 + 幂等注册
     │     ├─ _build_pipeline()
     │     └─ setup_run_logging() → sink_id
     ├─ if/elif granularity 分发        # 4.2：block / model / tensor
     │     └─ _run_blockwise() / _run_modelwise() / _run_tensorwise()
     └─ logger.remove(sink_id)          # 4.1：卸掉本次文件 sink
   ```

   并在图上用四种颜色/标记标出四个 Workflow 在「分发」那一步各自能走哪些分支（参考 4.2.2 的矩阵）。

2. **定位断点续跑逻辑**。PTQ Workflow 有一个其它三个都没有的特性：**已存在 param 文件就跳过该 unit**。请在 [llm_ptq.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py) 中找到它，答案是 `_run_blockwise()` 方法里的这段判断：
   [amct_pytorch/workflows/llm_ptq.py:191-198](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L191-L198)

   ```python
   if self._unit_result_path(unit) and os.path.exists(
       self._unit_result_path(unit)
   ):
       logger.info(f"Skip PTQ unit '{unit.name}' in layer {layer_idx}: "
                   f"params already exist at {self._unit_result_path(unit)}")
       continue
   ```

   它检查 `_unit_result_path(unit)` 指向的 `.pt` 文件是否已存在；存在则 `continue` 跳过这个 unit，不再重训。文件名由 [`_unit_result_path`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L241-L248) 按 `layer_{idx}_{save_name}.pt` 规则生成，落在 `args.quant_param_dir` 下。

3. **回答两个问题**（写在你的笔记里）：
   - 为什么 eval / extract / deploy 三个 Workflow **不需要**这种断点续跑逻辑？（提示：它们是「一次过」的前向 / 转换，没有「逐 unit 训练并落盘参数」的环节。）
   - PTQ 的断点续跑依赖 `quant_param_dir` 这个目录，它是在 `setup()` 的哪一步被确定的？（提示：[`_prepare_experiment_dirs`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L93-L101) → [`_resolve_quant_param_dir`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L103-L123)。）

**预期产出**：一张四 Workflow 共用的控制流图 + 一条指向 `llm_ptq.py:191-198` 的断点续跑标注 + 两个问题的简答。

---

## 6. 本讲小结

- 四个 Workflow（eval / extract_ptq_data / ptq / deploy）共用一套「`run()` → `setup()` → 按 granularity 分发 → `logger.remove(sink_id)`」的编排骨架，差异只在分发分支内部。
- `setup()` 内部固定四步：`_register_components()` → 准备目录 → `_build_pipeline()` → `setup_run_logging()`；注册必须排在建 pipeline 之前，因为取模型适配器依赖注册表。
- `--granularity` 是 Workflow 层的运行粒度（block/model/tensor），不要和 classic 量化算子的 strategy（tensor/channel/group）混淆；参数默认值是 `model`，但 PTQ/extract/deploy 实际都需显式传 `--granularity block`。
- 在 PTQ 里 granularity 还兼做 `SOLVER_REGISTRY` 的选型键（`block` → `BlockwiseSolver`），「一个开关同时决定执行路径和执行器类型」。
- `_register_components()` 采用惰性注册：不在 import 时触发，而在每次 `setup()` 里触发；靠各 `register_xxx()` 内部的 `_REGISTERED` 布尔位实现幂等，可安全重复调用。
- 四个 Workflow 注册的组件集合不同：extract 最轻（只 models），eval/deploy 居中（+dtype+algorithms），ptq 最全（+solvers），「按需装配件」。
- PTQ 独有的断点续跑逻辑在 `_run_blockwise()`（llm_ptq.py:191-198）：逐 unit 检查 `.pt` 参数文件是否已存在，存在即跳过，实现中断后重跑不重复训练。

---

## 7. 下一步学习建议

本讲只讲了 Workflow 的**外壳与分发**，刻意没碰分发后 `_run_blockwise()` 内部到底怎么逐层处理。建议按下面顺序继续：

1. **u3-l3 注册表驱动的插件架构**：本讲反复出现的 `MODEL_REGISTRY` / `SOLVER_REGISTRY` / `register_xxx` 到底怎么实现 `register/get/list_all` 与装饰器，去 `registry_factory.py` 一探究竟。
2. **u3-l4 BitPolicy 位宽配置**：`setup()` 里没展开的 `args.bit_policy` 如何从 yaml 解析出来。
3. **u4-l1 / u4-l2 PTQ 核心流程**：进入 `_run_blockwise()` 内部，看 PTQ 如何划分 `PtqUnit`、构建 batch、逐 unit 求解并落盘——本讲的断点续跑逻辑就是为这条链路服务的。
4. **u4-l4 部署导出**：进入 deploy 的 `_run_blockwise` / `_run_tensorwise`，看 safetensors 分片与 quantization_config 是如何生成的。

> 阅读建议：把本讲的「granularity 矩阵」和「四步 setup」两张表留在手边，后续读 u4 时你会不断回头对照——它们是整个 LLM PTQ 主链路的「目录页」。
