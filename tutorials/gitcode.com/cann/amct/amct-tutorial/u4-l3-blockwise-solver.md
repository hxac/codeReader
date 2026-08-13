# 块级重建优化 BlockwiseSolver

## 1. 本讲目标

本讲深入 AMCT 训练后量化（PTQ）链路中「真正做训练」的那一段：`BlockwiseSolver`。

学完本讲，你应当能够：

- 说清 `BaseSolver` 抽象骨架与 `BlockwiseSolver` 的「逐 block 重建」定位，理解 `solve()` → `_optimize_block()` 的两层循环。
- 解释 `_collect_trainable_param_groups()` 如何先冻结整层权重、再只把算法参数「点名」重新解冻，从而保证训练只动算法参数、原始权重始终不变。
- 推导 `_reconstruction_loss()` 的 MSE 目标，并能解释 `loss = loss / loss.clone().detach()` 这行「自归一化」代码的梯度含义与存在意义。
- 看懂 `build_optimizer()` / `build_lr_scheduler()` 如何从 CLI 参数（`--optimizer`、`--base_lr`、`--lr_scheduler`、`--epochs`）搭建优化器与学习率调度。

本讲承接 [u4-l2](u4-l2-ptq-main-flow.md)：在那里我们看到 `LlmPtqWorkflow._run_blockwise` 对每个 PtqUnit 调用了 `solver.solve(...)` 与 `solver.finalize()`，但把求解器内部当黑盒略过了。本讲就是打开这个黑盒。

## 2. 前置知识

阅读本讲前，请确认你已理解下列概念（均在前置讲义中讲过）：

- **重建（reconstruction）**：PTQ 的训练目标是「让量化后的子模块输出尽量逼近原始浮点子模块的输出」，优化对象是量化算法的可学习参数，原始权重冻结（见 u4-l2）。
- **PtqUnit**：最小量化工作单元，attn / mlp 各 1 个，MoE 每个 expert 1 个（见 u4-l2）。
- **DataLoader 契约**：每个 batch 形如 `(inputs, targets)`，`targets` 是浮点前向算出的 ground truth（见 u4-l2 的 `_prepare_unit_batch` / `materialize_gt`）。
- **注册表**：`@SOLVER_REGISTRY.register(...)` 装饰器副作用注册（见 [u3-l3](u3-l3-registry-architecture.md)）。
- **`trainable_params` 约定**：量化算法模块（继承 `QuantAlgorithmBase`）会实现 `trainable_params()` 方法，暴露自己想被训练的参数（见 u6-l1，本讲会再次用到）。

一个关键的 PyTorch 前置知识：`detach()` 会切断梯度回传，返回一个与计算图脱钩的同值张量；它常被用来「把一个值当常数用」。本讲最重要的那行代码就建立在它之上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [amct_pytorch/common/optimization/base_solver.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/base_solver.py) | `BaseSolver` 抽象基类，定义 `__init__` / 抽象 `solve` / `finalize` / `step` 等通用骨架。 |
| [amct_pytorch/common/optimization/blockwise_solver.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py) | `BlockwiseSolver`，本讲主角：block 粒度的重建优化循环。 |
| [amct_pytorch/common/optimization/factory.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/factory.py) | 工具函数：`set_require_grad_all` / `build_optimizer` / `build_lr_scheduler`。 |
| [amct_pytorch/common/optimization/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/__init__.py) | 定义 `SOLVER_REGISTRY` 与 `register_solvers()`，把 `BlockwiseSolver` 以 `name="block"` 注册。 |
| [amct_pytorch/workflows/llm_ptq.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py) | 调用方：`_run_blockwise` 里 `build_solver → solve → finalize → save`。 |
| [amct_pytorch/algorithms/quant/base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/base.py) | `QuantAlgorithmBase.trainable_params()` 的默认实现，本讲「参数点名」的来源。 |

## 4. 核心概念与源码讲解

### 4.1 BaseSolver 抽象骨架与 BlockwiseSolver 主循环

#### 4.1.1 概念说明

PTQ 的训练不是「训整个模型」，而是「把模型切成 PtqUnit，一个 unit 一个 unit 地训」。每个 unit 训练时，需要一个东西来驱动「前向 → 算损失 → 反向 → 更新参数」这个标准 PyTorch 训练循环。这个东西就是 **Solver（求解器）**。

`BaseSolver` 是所有 Solver 的抽象基类，它把「与具体粒度无关」的东西固化下来：

- 持有 `args`、`layer_idx`、`model`、`optimizer`、`lr_scheduler` 等状态。
- 提供 `finalize()`：训练结束后，把优化出来的参数导出。
- 提供 `step()`：优化器前进一格 + 学习率调度。

而把「按什么粒度组织训练循环」这件最关键的事，留给子类用抽象方法 `solve()` 去实现。

`BlockwiseSolver` 就是 `BaseSolver` 的 block 粒度实现，注册名 `name="block"`。回顾 u3-l2 提到的一个现象：**granularity 兼做 `SOLVER_REGISTRY` 选型键**——`block` → `BlockwiseSolver`。这里的根源就在它的注册名与类属性 `granularity = "block"`。

#### 4.1.2 核心流程

从调用方 `LlmPtqWorkflow._run_blockwise` 的视角，每个 PtqUnit 的处理是固定的四步：

```text
prepare_unit_batch (加载输入 + 算浮点 GT)
        │
        ▼
build_block_solver  ──►  solver = BlockwiseSolver(args, layer_idx, unit.module)
        │
        ▼
solver.solve(data_loader, forward_kwargs=...)   # 真正训练
        │
        ▼
solver.finalize()  ──►  导出训练好的算法参数 ──►  存 .pt
```

进入 `solve()` 后，`BlockwiseSolver` 的内部主循环是一个**两层嵌套**：

```text
solve(data_loader):
    if optimizer 未初始化:                       # 惰性构建（每个 unit 只建一次）
        model → device
        param_groups = _collect_trainable_param_groups(model)
        if param_groups 为空: return model       # 没有可学习参数，直接跳过
        optimizer     = build_optimizer(args, param_groups)
        lr_scheduler  = build_lr_scheduler(args, optimizer)
    for epoch in range(args.epochs):             # 外层：轮数
        avg_loss = _optimize_block(...)          # 内层：遍历所有 batch
        logger.info(...)                          # 打印该轮平均损失
```

`_optimize_block` 则是标准的单轮训练循环：遍历 DataLoader，每个 batch 做 `zero_grad → forward → loss → backward → step`。

#### 4.1.3 源码精读

先看注册与类定义。`BlockwiseSolver` 通过装饰器注册到 `SOLVER_REGISTRY`，名字是 `"block"`：

[amct_pytorch/common/optimization/blockwise_solver.py:32-45](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L32-L45) —— `@SOLVER_REGISTRY.register(name="block", ...)` 注册求解器；`__init__` 把 `block_size` / `max_iters` 等留作子类配置，并调用父类 `BaseSolver.__init__` 完成通用状态初始化。

再看 `BaseSolver` 的骨架，重点理解它固化了什么、留白了什么：

[amct_pytorch/common/optimization/base_solver.py:22-41](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/base_solver.py#L22-L41) —— `BaseSolver` 在 `__init__` 里保存 `args/layer_idx/model/optimizer/lr_scheduler/max_iters/current_iter`，`solve` 被标记为 `@abstractmethod`，强制子类实现。

`finalize()` 是训练结束后的「收尾」，它的优先策略是调用模型自己的 `export_ptq_params()`（如果模型实现了该方法），否则退化为「收集所有 `requires_grad=True` 的参数」：

[amct_pytorch/common/optimization/base_solver.py:47-55](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/base_solver.py#L47-L55) —— `finalize()` 导出训练产物，交给 workflow 落盘为 `.pt`（断点续跑的依据）。

`solve()` 的惰性构建值得单独点出：optimizer 不是在 `__init__` 里建的，而是在第一次 `solve()` 时按需建。原因是 `_collect_trainable_param_groups(self.model)` 需要遍历**已经被量化算子挂载好**的模型——这件事只有在 workflow 把 `QuantLinear` / 算法模块都挂上去之后才能做，所以推迟到 `solve()` 时刻正合适：

[amct_pytorch/common/optimization/blockwise_solver.py:47-64](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L47-L64) —— `solve()` 的「惰性建优化器 + epochs 外层循环」。注意第 51-52 行：若收集不到任何可训练参数，直接 `return self.model`，表示该 unit 没有可学习算法（比如只跑了纯 Min-Max），无需训练。

最后看内层循环 `_optimize_block`，它就是你能想象的最朴素的训练循环，唯一的「奇怪之处」是第 85 行那行归一化（我们在 4.3 节专门讲它）：

[amct_pytorch/common/optimization/blockwise_solver.py:66-92](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L66-L92) —— 遍历 DataLoader，对每个 `(inputs, targets)` 做前向 + MSE + 反向 + `step()`。第 76-77 行还顺手做了 batch 形状的硬校验：必须是二元组，否则报错。

> 小提示：第 80 行 `self.model(unit_inp, **kwargs)` 里的 `kwargs` 就是 `forward_kwargs`，即 u4-l1 中由 `Catcher` 在 embedding 阶段截获的 `attention_mask` / `position_ids` / `position_embeddings` 等，由 workflow 经 `unit_batch.kwargs` 透传进来。

#### 4.1.4 代码实践

**实践目标**：跟踪 `solve()` → `_optimize_block()` 的控制流，回答两个关于「行为」的问题。

**操作步骤**：

1. 打开 [blockwise_solver.py:47-64](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L47-L64) 与 [blockwise_solver.py:66-92](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L66-L92)。
2. 在 `_optimize_block` 入口处（第 67 行附近）**脑补**一行 `logger.debug(f"epoch batches={num_batches}")`（仅阅读，不改源码）。
3. 回答下面「需要观察的现象」。

**需要观察的现象 / 思考题**：

- 若某个 unit 没有任何可学习算法（`--algos` 没有给可学习算法），`_collect_trainable_param_groups` 会返回空列表。此时 `solve()` 在哪一行提前返回？`_optimize_block` 还会被调用吗？
- `optimizer` 是在 `__init__` 里建的，还是在 `solve()` 里建的？如果一个模型有 80 层、每层 1 个 unit，同一个 `BlockwiseSolver` 实例会被复用，还是每个 unit 新建一个？

**预期结果**：

- 空参数 → 在 [第 51-52 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L51-L52) `return self.model`，`_optimize_block` 不会被调用。
- optimizer 在 `solve()` 里惰性建。每个 unit 一个新的 `BlockwiseSolver` 实例（见 [llm_ptq.py:200](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L200) 的 `_build_block_solver` 在 unit 循环内被调用），所以「惰性建」其实每个 unit 触发一次。

#### 4.1.5 小练习与答案

**练习 1**：`BaseSolver.finalize()` 优先调用 `self.model.export_ptq_params()`，找不到该方法时才退化为「收集 `requires_grad=True` 的参数」。这两种路径分别会在什么情况下命中？

**参考答案**：当被优化的子模块（通常是包了一层量化逻辑的 `QuantLinear` 或更上层的 block wrapper）实现了 `export_ptq_params()` 时走第一条路径，它能精确控制要导出哪些参数（比如只导算法参数、排除无关 buffer）；当模型没有实现该方法（`hasattr` 为 False）时退回第二条路径，把所有 `requires_grad=True` 的参数一股脑导出——这正好对应 4.2 节里被「点名」解冻的那些算法参数。

**练习 2**：`BaseSolver.step()` 里 `optimizer.step()` 和 `lr_scheduler.step()` 的调用顺序是什么？如果 `lr_scheduler` 为 `None` 会发生什么？

**参考答案**：先 `optimizer.step()`（更新参数），再 `lr_scheduler.step()`（调整下一轮学习率）。`step()` 对 `self.lr_scheduler` 做了 `if` 判空（见 [base_solver.py:73](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/base_solver.py#L73)），为 `None` 时跳过调度——这正是 `build_lr_scheduler` 在 `scheduler_name=="none"` 时返回 `None` 的配合点。

---

### 4.2 _collect_trainable_param_groups：冻结权重，只挑算法参数

#### 4.2.1 概念说明

重建训练有一条铁律：**只许练算法参数，不许动原始权重**。原因有二：

1. 语义上，PTQ 是「在固定模型上找一组好的量化参数」，如果连原始权重都改了，那就不是 PTQ 而是微调了。
2. 工程上，原始 Linear 的 weight 矩阵动辄几亿参数，一旦 `requires_grad=True`，优化器会跟着它们一起更新，既慢又会破坏模型。

可是「算法参数」散落在模型各处的算法模块里（LWC / LAC / FlatQuant 等），怎么把它们精确地挑出来？AMCT 的办法非常优雅——**先一刀切冻结全部，再让算法模块自我举报名下的参数**。这个「自我报名」的接口就是 `trainable_params()`。

#### 4.2.2 核心流程

```text
_collect_trainable_param_groups(layer):
    1. set_require_grad_all(layer, False)        # 一刀切：全冻结（含原始 weight/bias）
    2. seen = set()                              # 用 id(param) 去重
    3. for module in layer.modules():
           fn = getattr(module, "trainable_params", None)
           if not callable(fn): continue         # 没有「报名」方法的模块直接跳过
           for param in fn():
               if param 已见过: continue
               param.requires_grad = True        # 只给算法参数重新「开灯」
               seen.add(id(param)); 收集
    4. return [{"params": [...], "lr": base_lr * 10}]
```

关键设计点：

- **冻结靠 blanket**：第 1 步 `set_require_grad_all(layer, False)` 把整层所有参数（原始 Linear 的 weight/bias、LayerNorm 的 affine、以及算法参数本身）统统冻结。
- **解冻靠点名**：第 3 步只对实现了 `trainable_params()` 的模块解冻它报名的参数。原始 `nn.Linear` 没有这个方法，`getattr` 拿到 `None`，被 `continue` 跳过——于是原始权重保持冻结。
- **去重靠 `id()`**：用对象 id 而非名字去重，防止同一个算法参数被多个 wrapper 重复报名而进优化器两次（进两次会导致梯度被累加两遍）。

#### 4.2.3 源码精读

`set_require_grad_all` 是个朴素的全量开关，对模型里每个参数无差别设置 `requires_grad`：

[amct_pytorch/common/optimization/factory.py:42-45](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/factory.py#L42-L45) —— 遍历 `named_parameters()`，统一设 `requires_grad`。这是「冻结原始权重」的物理实现。

接着是本节主角 `_collect_trainable_param_groups`，注意它如何用 `getattr(module, "trainable_params", None)` + `callable` 做「有则点名、无则跳过」：

[amct_pytorch/common/optimization/blockwise_solver.py:94-113](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L94-L113) —— 先全冻结，再只把实现了 `trainable_params()` 的模块所报名的参数重新解冻并去重收集，最后包成一个参数组，学习率设为 `base_lr * 10`。

`trainable_params()` 的默认实现在算法基类里，返回该算法模块自己的全部参数：

[amct_pytorch/algorithms/quant/base.py:35-36](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/base.py#L35-L36) —— `QuantAlgorithmBase.trainable_params()` 默认 `return list(self.parameters())`；子类如 LWC/LAC 只把自己定义的 `nn.Parameter`（`clip_factor_max/min`）登记为模块参数，所以 `self.parameters()` 恰好只含算法参数。

为了让你看到「算法参数」到底长什么样，这里给出 LWC 的参数定义作为例子（注意它们都是算法自己 `new` 出来的小张量，与原始 Linear 的 weight 无关）：

[amct_pytorch/algorithms/quant/auto_clip.py:29-43](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L29-L43) —— LWC 的 `clip_factor_max/min` 是形状 `(clip_dim, 1)` 的可学习参数，初始化为 `4.0`，用于在前向里经 `sigmoid` 调制截断边界。它们就是被 `_collect_trainable_param_groups`「点名」解冻的对象。

> 把 4.2.3 与 4.1.3 的 `finalize()` 串起来理解：训练时被解冻的是这些 `clip_factor_*`；训练结束后 `finalize()` 把它们导出成 `.pt`；下一轮 deploy 时再读回，作用到真实量化上。

#### 4.2.4 代码实践

**实践目标**：动手验证「原始权重被冻结、只有算法参数被解冻」。

**操作步骤**：

1. 打开 [blockwise_solver.py:94-113](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L94-L113)。
2. 阅读第 95 行（`set_require_grad_all(layer, False)`）与第 99-109 行（点名循环），在脑中模拟一个挂了 `QuantLinear` 的 block：原始 `nn.Linear` 会被哪一步冻结？`QuantLinear` 内部挂载的 LWC 算法的 `clip_factor_max` 又会被哪一步解冻？
3. 进阶（示例代码，无需运行）：用下面这段最小片段理解「点名」过滤效果——

   ```python
   # 示例代码：演示 trainable_params 的「有则报名、无则跳过」过滤
   import torch.nn as nn

   class Algo(nn.Module):           # 模拟 QuantAlgorithmBase
       def __init__(self):
           super().__init__()
           self.clip = nn.Parameter(torch.ones(4))
       def trainable_params(self):
           return list(self.parameters())

   block = nn.Sequential(nn.Linear(8, 8), Algo())  # 一个原始 Linear + 一个算法
   # set_require_grad_all(block, False) 之后：
   for m in block.modules():
       fn = getattr(m, "trainable_params", None)
       if not callable(fn):
           continue                 # nn.Linear 走这里被跳过 → 它的 weight 保持冻结
       for p in fn():
           p.requires_grad = True   # 只有 Algo.clip 被解冻
   ```

**需要观察的现象**：

- 上面示例里，`nn.Linear` 的 `weight` 最终 `requires_grad` 是 `True` 还是 `False`？`Algo.clip` 呢？
- 如果某个算法参数被两个 wrapper 同时引用（同一 `id`），`seen` 集合会如何防止它被加入参数列表两次？

**预期结果**：`nn.Linear.weight.requires_grad == False`（被第 1 步冻结，且没有 `trainable_params` 方法解冻它）；`Algo.clip.requires_grad == True`。重复引用时，第二次 `id(param) in seen` 命中、`continue`，参数列表里只出现一次。

> 待本地验证：若你想在真实 AMCT 上确认，可在 `solve()` 内临时打印 `sum(p.requires_grad for p in self.model.parameters())`，对比「解冻前后」的数量（仅阅读建议，不在本讲改源码）。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `id(param)` 去重，而不是用参数名 `name`？

**参考答案**：同一个参数对象可能被多个模块以不同名字引用（例如某个算法同时挂在 gate_proj 和 up_proj 上），用名字去重会误判为「不同参数」而重复收集；用 `id()` 针对的是对象身份，同一张量无论被几个名字引用都只收集一次，从而避免梯度被累加多次。

**练习 2**：`_collect_trainable_param_groups` 把所有算法参数放进**同一个**参数组，且学习率统一为 `base_lr * 10`。这种「不分组、不逐算法设学习率」的简化会带来什么权衡？

**参考答案**：好处是简单、无需为每个算法单独调学习率；代价是不同算法（比如 LWC 的截断因子与 FlatQuant 的正交矩阵）可能需要不同的学习率才能收敛最好，统一学习率意味着你只能靠 `--base_lr` 全局调。如果某算法需要单独调参，就得在子类里覆盖 `_collect_trainable_param_groups` 拆成多个参数组。

---

### 4.3 重建损失、归一化技巧与 optimizer/lr_scheduler 构建

#### 4.3.1 概念说明

训练目标非常直接：**让量化子模块的输出 `outputs` 逼近浮点子模块的输出 `unit_gt`**，二者都是张量，最自然的损失就是逐元素均方误差 MSE。这就是「重建」二字的本意——重建浮点输出。

但直接拿 MSE 反向传播有一个工程麻烦：**不同层、不同 unit 的 MSE 绝对值差异巨大**。一个 hidden_dim 很大的 FFN 输出，MSE 可能在 1e4 量级；而某个很小的 attention 输出 MSE 可能在 1e-3。如果用固定学习率，大损失层会产生超大梯度、训练发散甚至 NaN；小损失层则梯度太小、几乎不更新。AMCT 用一行非常巧妙的自归一化代码解决了这个问题。

#### 4.3.2 核心流程

重建损失定义：设量化输出为 \(\hat{y}\)、浮点 ground truth 为 \(y\)，则

\[
L_{\text{mse}} = \mathrm{mse\_loss}(\hat{y},\ y) = \frac{1}{N}\sum_{i=1}^{N}(\hat{y}_i - y_i)^2
\]

反传用的是「归一化损失」：

\[
\tilde{L} = \frac{L_{\text{mse}}}{\mathrm{sg}(L_{\text{mse}})}
\]

其中 \(\mathrm{sg}(\cdot)\) 表示 stop-gradient（即 PyTorch 的 `detach()`）。对参数 \(\theta\) 求导，因为 \(\mathrm{sg}(L_{\text{mse}})\) 在求导时被视为常数 \(L_{\text{mse}}\) 本身：

\[
\frac{\partial \tilde{L}}{\partial \theta}
= \frac{1}{\mathrm{sg}(L_{\text{mse}})} \cdot \frac{\partial L_{\text{mse}}}{\partial \theta}
= \frac{1}{L_{\text{mse}}}\, \nabla_\theta L_{\text{mse}}
\]

也就是说：

- **前向值**：\(\tilde{L}\) 永远等于 `1.0`（任何非零数除以自身为 1），与层的尺度无关——这就把「损失绝对值」从优化动力里剥离了。
- **梯度方向**：与原 MSE 完全一致（只是被正数 \(1/L_{\text{mse}}\) 缩放，不改变下降方向）。
- **梯度幅度**：被自动乘上 \(1/L_{\text{mse}}\)。损失越大，梯度被缩得越小——相当于**损失越大、有效学习率越小**，自带的「防爆炸」机制；不同尺度层的行为因此被拉到同一量级，一份 `--base_lr` 就能通吃。

一句话总结：**这行代码把「沿 MSE 梯度方向走一步」变成了「以单位损失的幅度走一步」，让优化对层的尺度不敏感。**

#### 4.3.3 源码精读

先看损失定义，它就是标准 MSE，没有花活：

[amct_pytorch/common/optimization/blockwise_solver.py:115-118](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L115-L118) —— `_reconstruction_loss` 返回 `torch.nn.functional.mse_loss(output, target)`，默认 `reduction='mean'`，输出标量。

再看内层循环里这行关键代码，请配合 4.3.2 的公式理解：

[amct_pytorch/common/optimization/blockwise_solver.py:83-87](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L83-L87) —— 第 83 行算 `loss`（真 MSE）；第 84 行把**真 MSE** detach 后累加进 `total_loss`（用于日志，反映真实下降幅度）；第 85 行 `loss = loss / loss.clone().detach()` 做自归一化后再 `backward()`。

> 注意第 84 行与第 85 行的分工：**日志用的是归一化前的真 MSE**（这样你能看到损失真实在下降），**反传用的是归一化后的 loss**（这样优化才稳定）。两者各司其职，不能混。
>
> 还有一个细节：用的是 `loss.clone().detach()` 而非 `loss.item()`。`.item()` 会强制一次 CPU 同步（host-device sync），拖慢训练；`detach()` 保持在原设备上、仍是张量，不触发同步，性能更好。

接着看优化器与学习率调度如何从 CLI 参数搭建。默认 `--optimizer adamw`、`--base_lr 1e-5`、`--lr_scheduler cosine`、`--epochs 15`：

[amct_pytorch/common/optimization/factory.py:48-66](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/factory.py#L48-L66) —— `build_optimizer` 支持 `adamw` / `adam` / `sgd`。注意 `adamw` 分支没有显式传 `lr`，用的是 PyTorch 默认值；但因为参数组在 `_collect_trainable_param_groups` 里已经设了 `"lr": base_lr * 10`，**参数组级别的 lr 会覆盖优化器默认 lr**——所以默认 adamw 实际生效的学习率是 `base_lr * 10 = 1e-4`。

[amct_pytorch/common/optimization/factory.py:69-96](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/factory.py#L69-L96) —— `build_lr_scheduler` 支持 `none` / `cosine` / `step`。默认 `cosine`：`T_max = epochs * (nsamples // cali_bsz)`，`eta_min = base_lr * 1e-3`，即余弦退火到接近 0。

把这套参数串起来对照 [amct_pytorch/cli/llm/args.py:96-114](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L96-L114) —— 默认 `cali_bsz=4`、`base_lr=1e-5`、`optimizer='adamw'`、`lr_scheduler='cosine'`、`epochs=15`，能看到「CLI 默认值 → factory 选择 → 实际优化行为」的完整链路。

#### 4.3.4 代码实践

**实践目标**：本讲指定的核心实践——解释「自归一化」与「冻结权重」两件事。

**操作步骤**：

1. 打开 [blockwise_solver.py:83-87](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L83-L87)，聚焦第 85 行 `loss = loss / loss.clone().detach()`。
2. 打开 [blockwise_solver.py:94-113](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L94-L113)，聚焦第 95 行与第 99-109 行。
3. 用下面的最小示例（**示例代码**，可直接复制到本地 Python 跑）验证归一化的梯度性质：

   ```python
   # 示例代码：验证 loss / loss.detach() 的前向值与梯度
   import torch

   x = torch.tensor([3.0], requires_grad=True)
   L = (x ** 2).mean()          # L = 9.0
   Lnorm = L / L.clone().detach()
   print(Lnorm.item())          # 期望: 1.0   —— 前向值归一
   Lnorm.backward()
   print(x.grad.item())         # 期望: 2*x / L = 6 / 9 = 0.6667
   # 对比: 直接对 L 反传, 梯度是 2*x = 6.0; 归一化后被 /L 缩放为 0.6667
   ```

**需要观察的现象 / 思考题**：

- 为什么用 `loss = loss / loss.clone().detach()` 这种归一化？（结合 4.3.2 的推导回答）
- `trainable_params` 机制如何保证只训练算法参数而冻结原始权重？（结合 4.2 的「先冻结、再点名」回答）

**预期结果**：

- 自归一化让前向 loss 恒为 1.0、梯度方向不变、梯度幅度被乘以 \(1/L\)，使不同尺度层的优化行为一致、防止大损失层梯度爆炸；同时日志仍记录真 MSE，便于观察收敛。
- `set_require_grad_all(layer, False)` 一刀切冻结全部 → 原始 `nn.Linear` 没有 `trainable_params` 方法被跳过而保持冻结 → 只有实现了 `trainable_params()` 的算法模块报名的参数被重新 `requires_grad=True`，故优化器只会更新算法参数。

> 待本地验证：示例代码的数值（`1.0` 与 `0.6667`）建议在本机运行确认；不同 PyTorch 版本浮点结果可能有极微小误差。

#### 4.3.5 小练习与答案

**练习 1**：如果把第 85 行换成 `loss = loss / loss.item()`，行为上还等价吗？为什么源码选择 `loss.clone().detach()`？

**参考答案**：数学上前向值都归一到 1.0、梯度方向一致，**但** `.item()` 会触发 CPU 同步（把当前 CUDA 流阻塞到该 op 完成），每个 batch 都同步一次会严重拖慢 NPU/GPU 训练；`loss.clone().detach()` 全程在设备上完成、不触发同步，性能更优。所以选 `detach()` 是工程考量，不是数学差异。

**练习 2**：默认配置下（`optimizer=adamw, base_lr=1e-5, lr_scheduler=cosine`），算法参数实际生效的初始学习率是多少？它是怎么来的？

**参考答案**：实际初始 lr 是 `1e-5 * 10 = 1e-4`。来源链：`_collect_trainable_param_groups` 在参数组里写死 `"lr": base_lr * 10`（[第 113 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L113)）→ `build_optimizer` 的 `adamw` 分支不传 lr（[第 53-54 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/factory.py#L53-L54)）→ PyTorch 用参数组 lr 覆盖优化器默认 lr。注意 `adam` / `sgd` 分支传的是 `base_lr`（未 ×10），与 adamw 存在 10× 差异，切优化器时需留意。

**练习 3**：`build_lr_scheduler` 在 `cosine` 模式下 `T_max = epochs * (nsamples // cali_bsz)`。为什么 `T_max` 要按「总 batch 数」算，而不是直接用 `epochs`？

**参考答案**：cosine 退火的横轴是「优化器 step 次数」。每个 epoch 会跑 `nsamples // cali_bsz` 个 batch、对应相同次数的 `step()`。要让学习率在最后一个 batch 恰好退火到 `eta_min`，`T_max` 必须等于总 step 数 = `epochs × 每 epoch 的 batch 数`。若直接用 `epochs`，退火会在前几个 batch 就提前结束，剩下一大半训练都用最低学习率，与预期不符。

## 5. 综合实践

把本讲三个最小模块串成一个完整的「求解器调参」思维实验。假设你负责量化某个 70 层的 LLM，跑完 PTQ 后发现：第 30 层 mlp 的 `avg_loss` 在 15 个 epoch 里几乎不下降，而其它层都正常收敛。

请你设计一个排查与调参方案，需要回答：

1. **先看 loss 是否被归一化干扰**：打开日志确认打印的 `avg_loss` 是真 MSE 还是归一化值（提示：看 [blockwise_solver.py:83-87](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L83-L87)，辨析 `total_loss` 累加的是哪个）。
2. **确认参数确实被解冻**：若 `avg_loss` 完全不动，怀疑算法参数没进优化器。请说明你会检查 `_collect_trainable_param_groups` 的哪一步——是「全冻结」过头，还是「点名」漏了？（提示：检查第 30 层的算法模块是否真的实现了 `trainable_params()`、其返回是否非空。）
3. **调学习率**：若参数进了优化器但收敛太慢，你会先调 `--base_lr` 还是 `--epochs`？注意默认 adamw 实际 lr 是 `base_lr*10`，改 `--base_lr` 会同时影响参数组 lr 与 cosine 的 `eta_min`，请说明这一联动。
4. **换优化器**：若决定换 `--optimizer adam`，根据 [factory.py:48-66](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/factory.py#L48-L66) 指出实际 lr 会从 `base_lr*10` 变为 `base_lr`（缩小 10×），你会如何相应调整 `--base_lr` 以保持训练强度？

这是一个纯源码阅读 + 推理的实践，无需运行真实量化。目标是让你把「参数收集 → 损失归一化 → 优化器/学习率」这条链路在脑子里跑通。

## 6. 本讲小结

- `BaseSolver` 固化通用骨架（状态、`finalize`、`step`），把粒度策略留白给子类的 `solve()`；`BlockwiseSolver` 是 `name="block"` 的 block 粒度实现，被 `SOLVER_REGISTRY` 按 granularity 选出。
- `solve()` 是「惰性建优化器 + epochs 外层循环」，`_optimize_block` 是标准的「zero_grad→forward→loss→backward→step」单轮循环；无可学习参数时第 51-52 行提前返回，跳过训练。
- `_collect_trainable_param_groups` 的核心是「先 `set_require_grad_all(layer, False)` 一刀切冻结全部，再让实现了 `trainable_params()` 的算法模块自我点名、用 `id()` 去重后重新解冻」——这是「只练算法参数、不动原始权重」的物理保证。
- `_reconstruction_loss` 是标准 MSE；第 85 行 `loss / loss.clone().detach()` 做自归一化，前向恒为 1.0、梯度方向不变、幅度被乘以 `1/L`，让不同尺度层的优化行为一致并防爆炸；日志则仍累加真 MSE 便于观察收敛。
- `build_optimizer` 默认 adamw（实际 lr 由参数组 `base_lr*10` 决定），`build_lr_scheduler` 默认 cosine（`T_max = epochs*(nsamples//cali_bsz)`）；CLI 默认值 `epochs=15 / base_lr=1e-5 / cali_bsz=4`。
- `finalize()` 把训练好的算法参数导出成字典，交给 workflow 存为 `.pt`，既是部署依据也是断点续跑的跳过判据（与 u4-l2 衔接）。

## 7. 下一步学习建议

本讲把 Solver 这一层讲透了，但有几件事被我们刻意当黑盒略过，正是后续讲义的主题：

- **算法模块到底实现了哪些接口**（`calib_forward` / `forward` / `trainable_params` / `export_ptq_params` / `load_ptq_params`，以及 `is_observe` 如何在它们之间切换通路）→ 读 [u6-l1 QuantAlgorithmBase 与 is_observe 通路](u6-l1-algo-base-observe.md)。
- **算法是如何按 weight/activation/structure 三类 target 被挂到不同模块上的**（决定了 `_collect_trainable_param_groups` 遍历 `layer.modules()` 时会碰到哪些算法）→ 读 [u6-l2 算法注册与 target 路由机制](u6-l2-algo-target-routing.md)。
- **`finalize()` 导出的参数如何被 deploy 阶段消费**、最终烘焙成可部署权重 → 读 [u4-l4 部署导出 deploy](u4-l4-deploy-export.md)。
- **`QuantLinear.forward` 在训练态如何调用这些算法参数**、以及 eval 缓存机制 → 读 [u7-l1 QuantLinear 与量化器模块](u7-l1-quant-modules.md)。

建议按 u6-l1 → u6-l2 → u7-l1 → u4-l4 的顺序读，把「训练出的算法参数如何变成部署产物」这条完整闭环补齐。
