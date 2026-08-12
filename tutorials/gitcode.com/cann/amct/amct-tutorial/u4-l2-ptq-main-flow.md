# PTQ 训练后量化主流程

## 1. 本讲目标

本讲深入 AMCT LLM 训练后量化（PTQ）四阶段链路（`eval → extract_ptq_data → ptq → deploy`）的**第三阶段 `ptq`** 的内部实现。

读者学完本讲应能：

1. 说清 `LlmPtqWorkflow._run_blockwise` 如何用「**逐层 → 逐 PTQ 单元**」两层循环完成整模型量化。
2. 理解 `PtqUnit` 这个数据结构为何是 PTQ 的最小工作单元，以及 `iter_ptq_units` 如何根据 `--quant_target` 把一层切成 1 个或多个单元（尤其 MoE 的「每个 expert 一个单元」）。
3. 掌握 `_prepare_unit_batch` 如何把上一阶段 `extract_ptq_data` 落盘的校准输入读回、生成浮点 ground truth、并包装成可训练的 `DataLoader`。
4. 看懂「逐单元求解 → `finalize` → 存 `.pt` → 断点续跑」这条贯穿始终的控制流。

本讲只讲「编排与数据」，**不**讲求解器内部的优化/损失细节（留给 u4-l3），也**不**讲算子挂载细节（留给 u5-l3）。

## 2. 前置知识

本讲假设你已掌握前置讲义中的以下概念（不会重复展开）：

- **四阶段链路与目录接力**（u1-l4）：`ptq` 阶段从 `--data_dir` 读回 `extract_ptq_data` 录制的中间激活，把求得的量化参数写到 `*_param_dir`，再由 `deploy` 读回。
- **extract_ptq_data 的落盘契约**（u4-l1）：extract 阶段把每个待量子模块的输入激活存成 `block_{layer_idx}_{target}_in.pkl`，其中 attn-linear/attn-cache 归并成 `attn`，mlp 存 `mlp`，moe 存 `moe`。
- **Workflow 三段式编排骨架**（u3-l2）：`run() → setup() → 按 granularity 分发 → logger.remove(sink_id)`；PTQ 的 granularity=block 同时兼做 `SOLVER_REGISTRY` 的选型键（`block → BlockwiseSolver`）。
- **注册表驱动**（u3-l3）：`MODEL_REGISTRY / SOLVER_REGISTRY / DTYPE_REGISTRY / ALGO_REGISTRY` 在 `setup()` 第一行惰性注册。

本讲会用到两个新术语：

- **PTQ 单元（PtqUnit）**：一层里可独立量化的最小子模块。一层 attention 算 1 个单元；一层 dense MLP 算 1 个单元；一层 MoE 则按 expert 数切成 N 个单元。
- **重建（reconstruction）**：PTQ 的训练目标不是缩小任务损失，而是让「量化后的子模块输出」逼近「原始浮点子模块输出」（ground truth）。所以每个单元在求解前，必须先用原始浮点权重跑一遍前向，把这份 reference 输出录下来作为 target。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `amct_pytorch/workflows/llm_ptq.py` | `LlmPtqWorkflow` 全部逻辑：入口构造、目录准备、`_run_blockwise` 主循环、断点续跑、保存结果 |
| `amct_pytorch/common/models/llm/common/ptq_units.py` | `PtqUnit` 数据类、`make_ptq_unit` 工厂、`iter_indexed_units` 按下标展开成多个单元的迭代器 |
| `amct_pytorch/common/datasets/ptq_provider.py` | `LlmPtqDataProvider`：读输入、生成 ground truth、构建 `DataLoader`；`BlockPtqBatch` 数据容器 |
| `amct_pytorch/common/models/llm/common/base.py` | `BaseModel` 提供 `iter_ptq_units`（按 quant_target 切单元）与 `build_quant_block`（构造量化层） |
| `amct_pytorch/common/datasets/ptq_io.py` | `load_ptq_inps` 读取 extract 阶段落盘的 `.pkl`，与本阶段 unit 的 `kind` 对齐 |

> 本讲引用的求解器入口（`BlockwiseSolver.solve/finalize`）来自 `amct_pytorch/common/optimization/blockwise_solver.py`，但其内部优化机制属于 u4-l3，本讲只用到它的外部契约。

## 4. 核心概念与源码讲解

### 4.1 PTQ 主流程定位与入口构造

#### 4.1.1 概念说明

`ptq` 命令的定位很纯粹：**逐层、逐子模块地训练量化参数**。它不碰原始浮点权重（原始权重始终冻结），只优化各算法挂上去的可学习参数（如截断因子、平滑因子、结构变换矩阵——具体是哪些算法见 u6-l4）。训练目标是「重建误差最小」：量化后子模块的输出要尽量贴近原始浮点子模块的输出。

入口类 `LlmPtqWorkflow` 与 eval/extract/deploy 三个兄弟 Workflow 共用一套三段式骨架（见 u3-l2），本节只讲它**区别于其他三者**的两点：

1. **只接受单个 `quant_target`**：一次 `ptq` 只能量化一种目标模块。
2. **granularity 兼做求解器选型键**：`SOLVER_REGISTRY.get(granularity)` 直接把粒度映射到求解器类。

#### 4.1.2 核心流程

```text
python -m amct_pytorch.ptq  (根目录薄壳, 见 u1-l4)
        │
        ▼
cli/llm/ptq.py: main()  →  parser_gen("ptq") → 构造 LlmPtqWorkflow(args) → run()
        │
        ▼
run():
   sink_id = setup()                          # 注册组件 / 建目录 / 建 pipeline / 建 data_provider / 挂日志
   solver_cls = SOLVER_REGISTRY.get(granularity)   # granularity=block → BlockwiseSolver
   results = self._run_blockwise(solver_cls)       # 主循环（granularity=model 当前未实现）
   logger.remove(sink_id)
```

#### 4.1.3 源码精读

构造函数在校验「单目标」约束后，把 `solver_key` 默认设为 `"blockwise"`：

[amct_pytorch/workflows/llm_ptq.py:L40-L50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L40-L50) —— `__init__` 中 `len(args.quant_target) != 1` 直接抛错，说明一次 `ptq` 只服务一个 target（与 extract 阶段必须一致，见 u4-l1）。

`run()` 的分发只区分 `block` 与 `model` 两条路：

[amct_pytorch/workflows/llm_ptq.py:L80-L91](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L80-L91) —— `solver_cls = SOLVER_REGISTRY.get(self.granularity)` 这一行就是 u3-l2 提到的「granularity 兼做选型键」；`_run_modelwise` 当前直接 `raise ValueError`，所以 PTQ 实际只走 block 这一条路。

`setup()` 内部四步是 u3-l2 讲过的稳定区，本讲关注它**准备的两个目录**：

[amct_pytorch/workflows/llm_ptq.py:L93-L101](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L93-L101) —— 建立 `log_dir` 与 `quant_param_dir`。后者是 PTQ → deploy 的接力目录：所有求解出的量化参数都存到这里，deploy 阶段再读回。

`quant_param_dir` 的具体属性名由 `quant_target` 决定：

[amct_pytorch/workflows/llm_ptq.py:L125-L134](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L125-L134) —— attn-linear → `attn_linear_param_dir`、attn-cache → `attn_cache_param_dir`、mlp/moe → `moe_mlp_param_dir`。若用户未指定，则自动建到 `<output_dir>/ptq_params/<model_name>/<quant_target>/`（见 `_resolve_quant_dir`，[L103-L123](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L103-L123)）。

#### 4.1.4 代码实践

**目标**：确认 PTQ 命令的入口分发与求解器选型，验证「granularity 即选型键」。

**步骤**：

1. 打开 [llm_ptq.py:L80-L91](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L80-L91)，找到 `solver_cls = SOLVER_REGISTRY.get(self.granularity)`。
2. 在 `amct_pytorch/common/optimization/` 下搜索 `SOLVER_REGISTRY.register(name="block"`，确认它注册的类就是 `BlockwiseSolver`。
3. 推断：若用户传 `--granularity model`，`run()` 会走到哪个分支、结果是什么。

**需要观察的现象 / 预期结果**：`--granularity model` 会命中 `_run_modelwise`，而该方法直接 `raise ValueError("Currently unsupported granularity 'model' for ptq.")`。结论：PTQ 当前强制走 block 粒度。运行命令需待本地验证（需要真实模型与 CANN 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LlmPtqWorkflow.__init__` 要强制 `len(args.quant_target) == 1`，而 extract 阶段也只接受单 target，二者一致性从何体现？

> **答**：因为单元划分（`iter_ptq_units`）、输入文件命名（`block_{layer_idx}_{kind}_in.pkl`）和参数目录（`*_param_dir`）全都以「当前 quant_target」为键。一次 PTQ 只针对一种子模块类型做参数求解，混用不同 target 会让落盘文件和单元语义错乱。extract 与 ptq 的 target 必须一致，根源在于二者共用同一套 `kind` 命名（见 4.4.3）。

**练习 2**：`solver_key = getattr(args, "solver", "blockwise")`（L50）这个字段在本讲范围内被用到了吗？

> **答**：没有被用到。实际的求解器类是通过 `SOLVER_REGISTRY.get(self.granularity)`（L83）拿到的，选型键是 granularity 而非 `solver_key`。`solver_key` 目前只是一个预留字段，本讲的 block 分支不依赖它。

---

### 4.2 `_run_blockwise`：逐层逐单元求解循环

#### 4.2.1 概念说明

`_run_blockwise` 是整条 PTQ 主链路的「心脏」。它解决一个工程问题：**大模型不可能整体塞进显存做训练**，所以必须把模型拆成一层一层（block）、再把每层拆成一个个子模块（unit），**每个 unit 独立求解、独立存盘**。

这样做带来三个直接收益：

1. **显存可控**：任一时刻只持有「一个 unit 的模块 + 它的校准数据」。
2. **可断点续跑**：每个 unit 求解完立刻存 `.pt`，重跑时已存在的跳过。
3. **MoE 友好**：一层 MoE 有几十个 expert，按 expert 切分后每个 expert 独立求解，互不干扰。

#### 4.2.2 核心流程

`_run_blockwise` 是「**层循环 + 单元循环**」的嵌套结构：

```text
for layer_idx in [start_block_idx, end_block_idx):        # 层循环
    block = pipeline.build_quant_block(layer_idx)         # 构造该层的「量化版」子模块树
    units = list(pipeline.iter_ptq_units(layer_idx, block))  # 切成若干 PtqUnit
    for unit in units:                                     # 单元循环
        if _unit_result_path(unit) 已存在:  continue       # 断点续跑：跳过已完成的
        unit_batch = _prepare_unit_batch(unit)            # 读输入 / 生成 GT / 构建 DataLoader
        solver = _build_block_solver(solver_cls, layer_idx, unit.module)
        solver.solve(unit_batch.data_loader, forward_kwargs=unit_batch.kwargs)  # 训练量化参数
        unit_result = solver.finalize()                   # 抽取学到的参数（不含原始权重）
        _save_unit_result(unit, unit_result)              # 存成 layer_{idx}_{unit}.pt
        释放 unit.module 回 CPU + empty_cache             # 显存回收
```

两层循环的关系：**层是模型结构维度**（第几层 decoder），**单元是量化目标维度**（这一层里要量化的具体子模块）。

#### 4.2.3 源码精读

完整主循环：

[amct_pytorch/workflows/llm_ptq.py:L172-L215](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L172-L215) —— 这是本讲最重要的一段代码，逐行职责如下：

- L174-L179：`end_block_idx` 超过模型层数时**钳制（clamp）**到 `num_layers`，并打 warning，避免越界。
- L182：`build_quant_block` 构造第 `layer_idx` 层的「量化版」block（内部已把目标 Linear 包成 `QuantLinear`，挂载细节见 u5-l3）。
- L183：`iter_ptq_units` 按 `quant_target` 把 block 切成单元列表（4.3 节详述）。
- L190-L198：**断点续跑核心**——`_unit_result_path(unit)` 返回的 `.pt` 若已存在则 `continue`，跳过本单元的全部计算。
- L199-L204：`_prepare_unit_batch` 喂数据，`_build_block_solver` 构造求解器，`solver.solve(...)` 执行重建训练。
- L206-L208：`finalize()` 抽出**只含可学习量化参数**的结果（不含原始权重），存盘。
- L210-L212：`unit.module.to("cpu")` + `del` + `torch.npu.empty_cache()`，把显存还回去。

求解器的构造用了**反射式依赖注入**，按求解器构造函数的形参名动态填参：

[amct_pytorch/workflows/llm_ptq.py:L222-L236](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L222-L236) —— 用 `inspect.signature` 读 `BlockwiseSolver.__init__` 的参数列表，只有形参名匹配 `args/layer_idx/model/block` 之一才传值。这样 Workflow 不必硬编码每个求解器的签名，新增求解器只要构造函数沿用这几个形参名即可被复用。

断点续跑的文件名生成：

[amct_pytorch/workflows/llm_ptq.py:L241-L248](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L241-L248) —— 文件名规则：有 `layer_idx` 时为 `layer_{layer_idx}_{save_name}.pt`，否则 `{save_name}.pt`。其中 `save_name` 是 `unit.name` 把 `.` 替换成 `_`（见 4.3.3），保证文件名合法。

存盘动作：

[amct_pytorch/workflows/llm_ptq.py:L250-L258](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L250-L258) —— 一行 `torch.save(result, save_path)`，把 `finalize()` 的返回值整个序列化。这份 `.pt` 就是 deploy 阶段的输入之一。

#### 4.2.4 代码实践

**目标**：验证断点续跑行为，理解它「以 unit 为粒度」而非「以 layer 为粒度」。

**步骤**：

1. 读 [L190-L198](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L190-L198)，确认跳过判断发生在**单元循环内部**、`_prepare_unit_batch` **之前**。
2. 假设一个 3-expert 的 MoE 模型第 5 层已跑完 `expert_0`、`expert_1`，但 `expert_2` 还没跑就中断了。问：重跑时第 5 层会重新训练几个单元？
3. 想象一下：如果要把断点粒度从 unit 改成 layer（一层要么全跑要么全跳），需要移动哪几行代码？

**需要观察的现象 / 预期结果**：第 5 层重跑时只训练 `expert_2` 一个单元（`expert_0/1` 的 `.pt` 已存在被跳过）。这正是 unit 粒度断点的价值——MoE 一层几十个 expert 时，中断一个不浪费其余。若改成 layer 粒度，需把 L190-L198 的判断挪到 L183 之后、L190 之前（层循环内、单元循环外），代价是 `expert_2` 已存的算力被浪费。命令运行需待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`solver.solve()` 执行的是「重建训练」，为什么 PTQ 阶段不直接用下游任务的 loss（如语言模型交叉熵），而要用重建 loss？

> **答**：PTQ 是**逐子模块局部**训练（一次只看一个 unit 的输入输出），根本没有完整的 forward graph 跑到 logits，无法算交叉熵。重建 loss 只要求「量化前后这个子模块的输出尽量一致」，是局部可计算的目标，且已被 AWQ/GPTQ/SmoothQuant 等算法验证足以保住下游精度。具体 loss 形式见 u4-l3。

**练习 2**：L212 调用的是 `torch.npu.empty_cache()` 而不是 `torch.cuda.empty_cache()`，这说明了什么？

> **答**：AMCT 运行在昇腾 NPU 上，设备后端是 `torch_npu` 提供的 `npu` 而非 CUDA。这与 u1-l2 讲的「装 CPU 版 torch + torch_npu」环境是一致的——显存回收走 NPU 的 API。

---

### 4.3 `PtqUnit` 数据结构与单元划分 `iter_ptq_units`

#### 4.3.1 概念说明

`PtqUnit` 是 PTQ 流程的**工作票据**：它把「要在哪个子模块上做什么量化」打包成一个不可变的数据对象。主循环里 `units` 列表的每一个元素就是一个 `PtqUnit`。

为什么需要这个抽象？因为不同 `quant_target` 切出来的单元数量和形状完全不同：

| quant_target | 一层切出几个 unit | unit.module 是什么 |
| --- | --- | --- |
| `attn-linear` / `attn-cache` | 1 个 | 整个 attention 子模块（`self_attn` 或 `linear_attn`） |
| `mlp` | 1 个 | 整个 dense MLP |
| `moe` | N 个（每 expert 一个） | 每个 expert 各自的 `QuantGatedMLP` |

把这种差异**收敛到统一的 `PtqUnit` 接口**后，`_run_blockwise` 的单元循环就能用同一套代码处理所有 target——它只看 `unit.module / unit.name / unit.layer_idx`，不关心具体是 attention 还是 expert。

#### 4.3.2 核心流程

单元划分逻辑分两类：

- **单单元 target**（attn / mlp）：直接 `make_ptq_unit` 造一个单元，`module` 指向 block 上对应的子模块。
- **多单元 target**（moe）：调用 `iter_indexed_units`，遍历所有 expert，给每个 expert 造一个单元，命名 `expert_0 / expert_1 / ...`，并在 `metadata` 里记下 `expert_idx`。

MoE 的关键细节：experts 容器有两种暴露方式——优先调 `iter_ptq_expert_modules()`（按需构造、省内存），退而求其次取 `expert_modules` 属性（已构造好的列表）。

#### 4.3.3 源码精读

`PtqUnit` 是个极简 dataclass：

[amct_pytorch/common/models/llm/common/ptq_units.py:L26-L36](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_units.py#L26-L36) —— 五个字段：`kind`（attn/mlp/moe，**用于读输入文件**）、`name`（单元名，**用于命名结果文件**）、`layer_idx`、`module`（待量化子模块实例）、`metadata`（如 expert_idx）。`save_name` 属性把 `name` 里的 `.` 换成 `_`，确保落盘文件名合法。

工厂函数与按下标展开的迭代器：

[amct_pytorch/common/models/llm/common/ptq_units.py:L39-L48](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_units.py#L39-L48) —— `make_ptq_unit` 工厂，把 `metadata=None` 归一化成 `{}`。

[amct_pytorch/common/models/llm/common/ptq_units.py:L51-L70](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_units.py#L51-L70) —— `iter_indexed_units`：`for idx, item in enumerate(items)`，逐个 yield `PtqUnit(name=f"{name_prefix}_{idx}")`。两个可选回调：`module_fn` 决定每个 item 取出什么模块（不传则 `module=item` 本身），`metadata_fn` 给每个单元附加元数据（MoE 用它记 `expert_idx`）。`module is None` 时跳过，容错缺失的 expert。

`BaseModel.iter_ptq_units` 把三种 target 的切分逻辑收拢到一处：

[amct_pytorch/common/models/llm/common/base.py:L293-L322](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L293-L322) —— 三条分支：

- L294-L301：attn-linear/attn-cache → 1 个 `kind="attn"` 单元。注意它还兼容 `layer_type == "linear_attention"` 的模型（取 `linear_attn` 而非 `self_attn`）。
- L306-L319：moe → 走 `iter_indexed_units(kind="moe", name_prefix="expert", ...)`，`metadata_fn=lambda expert_idx, _: {"expert_idx": expert_idx}`。**所有 expert 共享同一个 `kind="moe"`**，这点对 4.4 节读输入至关重要。
- L320-L322：mlp → 1 个 `kind="mlp"` 单元。

MoE expert 的两种暴露方式（按需构造优先）：

[amct_pytorch/common/models/llm/qwen/moe_common.py:L69-L71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py#L69-L71) —— `iter_ptq_expert_modules` 每次 yield 调 `build_ptq_expert_module(expert_idx)` 现场构造一个 `QuantGatedMLP`（`materialize=True`），避免一次性把所有 expert 都物化到内存。

#### 4.3.4 代码实践

**目标**：动手模拟 `iter_indexed_units`，理解它如何把 MoE 的 expert 列表展开成单元。

**步骤**：在本地 Python（无需 NPU）跑下面这段**示例代码**（非项目原码，仅用于演示 `iter_indexed_units` 的行为）：

```python
# 示例代码：模拟一个 3-expert 的 MoE 层被切成 PtqUnit
from amct_pytorch.common.models.llm.common.ptq_units import iter_indexed_units

fake_experts = ["expert_module_0", "expert_module_1", "expert_module_2"]
units = list(iter_indexed_units(
    kind="moe",
    name_prefix="expert",
    layer_idx=5,
    items=fake_experts,
    metadata_fn=lambda idx, _: {"expert_idx": idx},
))
for u in units:
    print(u.kind, u.name, u.layer_idx, u.module, u.metadata, "->", u.save_name)
```

**需要观察的现象 / 预期结果**：输出 3 个单元，`name` 分别为 `expert_0/1/2`，`module` 就是列表里的字符串（因为没传 `module_fn`），`metadata` 各自带 `{"expert_idx": 0/1/2}`，`save_name` 与 `name` 相同（无 `.`）。注意每个单元的 `kind` 都是 `"moe"`——它们将共享同一份输入文件（见 4.4）。

#### 4.3.5 小练习与答案

**练习 1**：`iter_ptq_units` 对 MoE 切出的所有单元 `kind` 都是 `"moe"`，但 `name` 各不相同。`kind` 和 `name` 分别被主流程用在什么地方？

> **答**：`kind` 用在读输入（`load_unit_inputs` → `load_ptq_inps(data_dir, unit.kind, ...)`，决定读哪个 `.pkl`），所有同层 expert 共用 `block_{layer_idx}_moe_in.pkl`；`name` 用在命名结果文件（`_unit_result_path` → `layer_{layer_idx}_{save_name}.pt`），每个 expert 各自一份 `.pt`。

**练习 2**：为什么不直接把所有 expert 物化成 `expert_modules` 列表，而要提供 `iter_ptq_expert_modules()` 这种惰性迭代器？

> **答**：MoE 一层可能有几十甚至上百个 expert，一次性全部物化成 `QuantGatedMLP` 会占大量内存/显存。`iter_ptq_expert_modules` 按需构造一个、用一个、释放一个，配合 `_run_blockwise` 末尾的 `module.to("cpu") + empty_cache`，把峰值显存压到「单个 expert」级别。

---

### 4.4 数据 batch 构建：`_prepare_unit_batch` / `materialize_gt` / `build_unit_batch`

#### 4.4.1 概念说明

求解器要训练，就得喂它「输入 + 目标」成对的 mini-batch。PTQ 的 batch 由三步拼出来：

1. **读输入**：从 `--data_dir` 把 extract 阶段录的该单元输入激活读回（就是 u4-l1 落盘的那些 `.pkl`）。
2. **生成 ground truth**：用**原始浮点**子模块对这份输入跑一次前向，把输出存为 target。注意，此时量化包装器必须切到「observe 态」——只统计不伪量化，等价于浮点直通，这样录到的 target 才是真正的浮点 reference。
3. **打包**：把 `(input, target)` 拼成 `TensorDataset`，套上 `DataLoader`，按 `--cali_bsz` 分批。

这里有一个**关键的命名对齐**：ptq 读输入用的键是 `unit.kind`（attn/mlp/moe），这必须和 extract 阶段存盘时的 `save_target` 完全一致——这正是 extract 与 ptq 两阶段 `--quant_target` 必须相同的文件层根源。

#### 4.4.2 核心流程

`_prepare_unit_batch` 把三步串起来，其中 observe 开关的 try/finally 是重点：

```text
load_unit_inputs(unit)                          # 读 block_{layer_idx}_{unit.kind}_in.pkl + kwargs
inps, kwargs = 拆包
unit.module = unit.module.float().to(device)
set_model_to_observe(unit.module, True)         # 开启 observe：量化器只统计、不伪量化
try:
    gts = materialize_gt(inps, unit.module, kwargs)   # 跑浮点前向 → ground truth
finally:
    set_model_to_observe(unit.module, False)    # 关闭 observe：之后 solve() 走真量化
return build_unit_batch(unit, inps, kwargs, gts)      # (inps, gts) → DataLoader
```

ground truth 的产生可形式化为：对单元输入 \(x\)，记原始浮点子模块为 \(f\)，则

\[
y = f(x), \qquad \mathcal{L} = \| Q_{\theta}(x) - y \|^2
\]

其中 \(Q_{\theta}\) 是带可学习参数 \(\theta\) 的量化版子模块，\(\theta\) 就是 PTQ 要优化的对象（原始权重始终冻结）。

#### 4.4.3 源码精读

Workflow 侧的编排：

[amct_pytorch/workflows/llm_ptq.py:L156-L170](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L156-L170) —— L157 读输入；L162-L164 把模块与数据都搬到 NPU 并转 float32（训练需 float32 精度）；L165-L169 的 try/finally **保证 observe 一定被关掉**，哪怕生成 GT 时抛异常也不会让量化器卡在统计态；L170 把准备好的数据打包。

读输入落到 ptq_io：

[amct_pytorch/common/datasets/ptq_provider.py:L48-L49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L48-L49) —— `load_unit_inputs` 委托给 pipeline。

[amct_pytorch/common/models/llm/common/base.py:L80-L82](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L80-L82) —— `BaseModel.load_unit_inputs` 直接调 `load_ptq_inps(data_dir, unit.kind, unit.layer_idx)`，**用 `unit.kind` 作文件名键**。

[amct_pytorch/common/datasets/ptq_io.py:L42-L63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L42-L63) —— `load_ptq_inps` 读 `block_{layer_idx}_{quant_target}_in.pkl`；只有 `kind == "attn"` 时才额外读 `position_ids / position_embeddings / attention_mask` 三个 kwargs（因为 attention 前向需要位置信息，MLP/MoE 不需要）。文件缺失返回 `(None, kwargs)` 而非报错，留给上层处理。

> 对齐验证：extract 阶段 [ptq_io.py:L35-L39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L35-L39) 存盘时 `save_target` 同样把 attn-linear/attn-cache 归并成 `attn`，其余取 `quant_target[0]`，与本阶段 `unit.kind` 一一对应。

生成 ground truth：

[amct_pytorch/common/datasets/ptq_provider.py:L73-L91](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L73-L91) —— `materialize_gt`：先把模块转 float+eval，按 `cali_bsz` 分批跑前向（`torch.no_grad()`），每批输出 `detach()` 后收集，最后 `torch.cat(gts, dim=0)` 拼回完整 `[num_samples, ...]` 张量。注意它跑的是**已被量化包装器包过的 `unit.module`**，但因为 observe=True，量化器走直通，输出即等价浮点结果。observe 的精确通路机制见 u6-l1。

打包成 DataLoader：

[amct_pytorch/common/datasets/ptq_provider.py:L51-L71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L51-L71) —— `build_unit_batch`：`tensors = [inps]`，有 GT 则 `tensors.append(gts)`；`TensorDataset(*tensors)` 配 `DataLoader(batch_size=cali_bsz, shuffle=False, num_workers=0)`。`shuffle=False` 是为了保证 input 与 GT 的对齐顺序不被打乱。返回的 `BlockPtqBatch` 是个简单数据容器：

[amct_pytorch/common/datasets/ptq_provider.py:L29-L37](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L29-L37) —— 携带 `data_loader / kwargs / num_samples / has_gts / metadata`，其中 `kwargs`（attn 的位置参数）会一路透传到 `solver.solve(forward_kwargs=...)`。

#### 4.4.4 代码实践

**目标**：理解「所有 MoE expert 共享同一份输入、各自不同的 GT」这一数据流事实。

**步骤**：

1. 读 [base.py:L80-L82](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L80-L82) 与 [ptq_io.py:L57](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L57)，确认 `load_ptq_inps` 的文件名只依赖 `unit.kind`（moe）和 `layer_idx`，**不含 expert_idx**。
2. 回看 4.3 的单元划分：同层 N 个 expert 的 `unit.kind` 全是 `"moe"`、`layer_idx` 全相同。
3. 推断：第 5 层的 `expert_0` 和 `expert_1` 调 `load_unit_inputs` 时，读到的是不是同一份 `block_5_moe_in.pkl`？那它们各自的 ground truth 又为何不同？

**需要观察的现象 / 预期结果**：两者读到**同一份输入张量**（因为输入是「进入 experts 模块的激活」，对所有 expert 公共）；但 GT **各不相同**，因为 `materialize_gt` 是在各自的 `unit.module`（不同 expert 的权重）上跑前向——同输入、不同权重、不同输出。这解释了为何存盘用 `name`（expert_0/1）区分、而读输入只用 `kind`（moe）。命令运行需待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`_prepare_unit_batch` 里 observe 开关为什么必须用 try/finally，而不仅是 try？

> **答**：若 `materialize_gt` 抛异常（如显存不足、形状不匹配），没有 finally 的话 observe 会一直停在 True，后续的 `solve()` 走的还是统计直通态、根本没在做量化训练，却不会报错——这是一种隐蔽的「静默失效」。finally 保证无论成败 observe 都被关回 False，让后续真量化或异常都暴露出来。

**练习 2**：`build_unit_batch` 里 `shuffle=False`，如果改成 `shuffle=True` 会怎样？

> **答**：`TensorDataset` 把 `inps` 与 `gts` 按同一索引配对，`shuffle=True` 会**同步打乱两者**（DataLoader 对 dataset 的 shuffle 保持索引一致），所以配对关系不会错。但 PTQ 追求可复现，且校准样本顺序本身无随机性要求，故显式 `shuffle=False` 以保证每次跑出的量化参数一致。

---

## 5. 综合实践

**任务**：端到端追踪一个 `--quant_target moe` 的 PTQ 单元，把本讲四个模块串起来。

设模型有 2 层、每层 3 个 expert，`--start_block_idx 0 --end_block_idx 2`，`quant_param_dir` 已配置。请按顺序回答并画出数据流：

1. **单元生成**：打开 [base.py:L306-L319](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L306-L319)。第 1 层会切出几个单元？写出每个单元的 `kind / name / layer_idx / metadata`（参考 [ptq_units.py:L51-L70](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_units.py#L51-L70) 的 `iter_indexed_units`）。
2. **断点续跑文件名**：对第 1 层的每个 expert 单元，用 [llm_ptq.py:L241-L248](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L241-L248) 推出它会去检查哪个 `.pt` 文件是否存在。
3. **输入读取**：这些 expert 单元调 [base.py:L80-L82](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L80-L82) 时，分别读哪个 `.pkl`？是同一份还是不同份？
4. **数据流图**：画一张图，标出「extract 落盘的 `.pkl` → ptq 读入 → materialize_gt 生成各 expert 的 GT → DataLoader → solve → finalize → 落盘 `.pt`」全链路，并在图上标注「哪些是按层共享、哪些是按 expert 独立」。

**参考答案要点**：

1. 第 1 层切出 3 个单元：`{kind:"moe", name:"expert_0", layer_idx:1, metadata:{"expert_idx":0}}`、`expert_1`、`expert_2`。
2. 文件名分别为 `layer_1_expert_0.pt`、`layer_1_expert_1.pt`、`layer_1_expert_2.pt`（注意 `save_name` 把 `.` 换 `_`，此处无 `.`）。
3. 三个单元**都读同一份** `block_1_moe_in.pkl`（文件名只含 `kind="moe"` 和 `layer_idx=1`，不含 expert_idx）；attn 的位置 kwargs 不读（非 attn target）。
4. 数据流图要点：`.pkl`（按层共享，1 份）→ 读入同一 `inps` → 每个 expert 各自 `materialize_gt` 产出**不同** GT（3 份）→ 3 个独立 `DataLoader` → 3 次 `solve` → 3 个 `finalize` 结果 → 3 份 `.pt`（按 expert 独立）。共享的是「输入」，独立的是「模块权重、GT、结果参数」。

## 6. 本讲小结

- `ptq` 阶段的本质是**逐层、逐 PtqUnit 地做重建训练**：让量化子模块的输出逼近原始浮点子模块的输出，只优化算法的可学习参数，原始权重始终冻结。
- `_run_blockwise` 是「层循环 + 单元循环」的嵌套，每个 unit 独立走 `prepare_batch → solve → finalize → save`，并在末尾回收显存。
- `PtqUnit` 是统一的单元票据，把 attn（1 个）/ mlp（1 个）/ moe（N 个 expert）的差异收敛到同一接口；`iter_indexed_units` 负责把 MoE 的 expert 列表按下标展开。
- 断点续跑以 **unit 为粒度**：`_unit_result_path` 生成 `layer_{idx}_{save_name}.pt`，存在即跳过，MoE 中断一个 expert 不浪费其余。
- 数据流核心对齐：ptq 用 `unit.kind` 读 extract 落盘的 `block_{layer_idx}_{kind}_in.pkl`，所以两阶段的 `--quant_target` 必须一致；同层所有 MoE expert 共享一份输入，但各有独立的 GT 与 `.pt`。
- `_prepare_unit_batch` 用 observe 开关（try/finally）保证生成 GT 时量化器走浮点直通、之后切回真量化；求解器通过反射式依赖注入构造，新增求解器只要形参名对齐即可复用。

## 7. 下一步学习建议

- **求解器内部**：本讲把 `solver.solve / finalize` 当黑盒用，它的重建 loss 如何定义、可学习参数如何收集、optimizer/lr_scheduler 怎么建，请看 **u4-l3 块级重建优化 BlockwiseSolver**。
- **部署导出**：本讲产出的 `.pt` 参数如何被读回并烘焙成可部署权重，请看 **u4-l4 部署导出 deploy**。
- **模型适配底层**：`build_quant_block` 如何把原始 Linear 包成 `QuantLinear`、`iter_ptq_expert_modules` 的 expert 视图如何工作，请看 **u5-l1 LLM 模型适配基类 BaseModel** 与 **u5-l3 量化算子挂载 quant_apply**。
- **observe 机制**：`set_model_to_observe` 在量化器内部如何切换「统计态/量化态」两条通路，请看 **u6-l1 QuantAlgorithmBase 与 is_observe 通路**。
