# 学习率调度器：从 warmup 到余弦退火

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 OLMo-core 的调度器为什么**不是** `torch.optim.lr_scheduler`，而是一套自研的、基于 Config + Registrable 的体系。
2. 跟踪一次 `optim_step` 中学习率被更新的完整链路：`Scheduler.set_lr()` → 读取 Trainer 状态 → 写回参数组。
3. 手算 `CosWithWarmup` 在任意 step 的学习率：线性 warmup 段、余弦衰减段、`alpha_f` 下限各是什么。
4. 独立绘制一条完整的学习率曲线，并对照 OLMo-2 / OLMo-3 官方脚本的真实参数。
5. 说清楚 OLMo-3 预训练第二阶段（anneal）为什么要把 `CosWithWarmup` 换成 `HalfCosWithWarmup` 并重设 `lr`。

## 2. 前置知识

**学习率为什么要「调度」。** 优化器每一步沿梯度反方向更新参数，步长就是学习率（learning rate, LR）。训练初期参数离最优解很远、梯度的统计噪声也大，太大的 LR 会让损失发散或剧烈震荡，所以要先「热身」（warmup）——从一个很小的 LR 线性爬升到峰值；训练后期接近收敛，大步长反而来回跳过最优点，所以要把 LR「退火」（anneal/decay）下来，让模型稳定收尾。warmup + 衰减共同构成一条学习率曲线。

**参数组（param groups）。** 承接上一讲（u5-l1）：PyTorch 优化器把参数分成若干组，每组在 `group` 字典里各有自己的 `lr`、`weight_decay` 等超参。调度器的工作就是**在每个优化步之前，把每个组里的 `lr` 改成本步应有的值**。上一讲提到的 `OptimGroupOverride` 决定「哪些参数进哪个组」，本讲的调度器决定「每组此刻的 lr 是多少」。

**`initial_lr` 与 `fixed_fields`。** 上一讲我们看到 `OptimConfig` 会给每个参数组钉上一个 `initial_lr`（峰值学习率快照），并把它放进 `fixed_fields`、随 checkpoint 保存。本讲会看到调度器正是以 `initial_lr` 为基准来计算即时 LR 的——这保证了「曲线形状」与「从哪个 checkpoint 恢复」无关。

**Registrable 多态。** 承接 u2-l2：调度器基类 `Scheduler` 混入了 `Registrable`，每个具体调度器用 `@Scheduler.register("名字")` 注册。于是在 YAML 或命令行里写 `--train_module.optim.scheduler.type=wsd`（外加对应字段）就能整体替换调度器，无需改代码。

**调用时机。** 承接 u1-l4：训练主循环每个 step 依次执行 `train_batch`（前向+反向）→ `optim_step`（更新参数）→ `zero_grads`。本讲的调度器就插在 `optim_step` 内部、`optim.step()` 之前。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/optim/scheduler.py](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py) | 全部调度器实现：`Scheduler` 基类 + 14 个具体调度器 + warmup/decay 辅助函数 |
| [src/olmo_core/optim/\_\_init\_\_.py](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/__init__.py) | 门面：从 `scheduler` 子模块再导出全部调度器类与 `LR_FIELD`/`INITIAL_LR_FIELD` |
| [src/olmo_core/optim/config.py](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/config.py) | 定义 `LR_FIELD = "lr"`、`INITIAL_LR_FIELD = "initial_lr"` 常量与 `fixed_fields` 默认值 |
| [src/olmo_core/train/train_module/transformer/train_module.py](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/train/train_module/transformer/train_module.py) | `optim_step()`：训练循环中调用 `scheduler.set_lr()` 的唯一位置 |
| [src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py) | OLMo-2 32B 官方脚本：`CosWithWarmup(warmup_steps=2000)` |
| [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py) / [-pretrain-2.py](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py) / [-midtrain.py](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo3/OLMo-3-1025-7B-midtrain.py) | OLMo-3 7B 三个阶段的调度器选择：`CosWithWarmup` → `HalfCosWithWarmup` → `LinearWithWarmup` |
| [src/test/optim/scheduler_test.py](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/test/optim/scheduler_test.py) | 各调度器的单元测试，含用 Mock Trainer 驱动 `set_lr` 的集成测试 |

## 4. 核心概念与源码讲解

### 4.1 调度器抽象：`Scheduler` 基类与 `set_lr`

#### 4.1.1 概念说明

PyTorch 自带 `torch.optim.lr_scheduler.LambdaLR` 等调度器，为什么 OLMo-core 要自己造一套？因为库的设计主题是「一切皆 Config」：

- 每个调度器都是一个可序列化、可 Registrable 的 dataclass，能直接嵌进 `TransformerTrainModuleConfig.scheduler` 字段，随实验配置一起存入 checkpoint 的 `config.json`（承接 u2-l3 的可复现闭环）；
- 计算接口是纯函数式的 `get_lr(initial_lr, current, t_max)`——只依赖峰值学习率、当前进度和总进度，**不依赖优化器对象**，因此可以脱离训练单独测试、单独画曲线；
- 副作用被隔离在 `set_lr()` 一个方法里，由训练模块在每个优化步统一调用。

基类还提供一个容易被忽视的配置：`units`（`steps` 或 `tokens`）。用 step 计数还是 token 计数做横轴，是调度器自己的选择，与 `Duration` 的停止条件（u6-l1）互相独立。

#### 4.1.2 核心流程

`set_lr(group, trainer)` 在每个优化步、每个参数组上执行一次：

1. 检查 `group` 里至少有 `lr` 或 `initial_lr` 之一，否则抛 `RuntimeError`（提示你检查参数组是否被正确构建）。
2. 若 `initial_lr` 缺失，用当前 `lr` 补上（正常情况下 u5-l1 的 `OptimConfig.build()` 已经设置好了）。
3. 按 `units` 取进度：
   - `steps`：`current = trainer.global_step`，`t_max = trainer.max_steps`；`max_steps` 未知则抛 `OLMoConfigurationError`；
   - `tokens`：`current = trainer.global_train_tokens_seen`，`t_max = trainer.max_tokens`。
4. 调用子类的 `get_lr(initial_lr, current, t_max)` 算出新 LR。
5. 写回：若组里的 `lr` 是张量（FSDP 等场景下 lr 可能被搬上设备），就地 `fill_`；否则直接赋值。

用伪代码概括整条链路：

```
Trainer 循环（每 step）
  └─ TransformerTrainModule.optim_step()
       ├─ 遍历 optim.param_groups:
       │     scheduler.set_lr(group, trainer)   # 本讲
       │           └─ get_lr(initial_lr, global_step, max_steps)
       ├─ optim.step()                          # 用新 lr 更新参数
       └─ ...
```

一个关键设计：曲线**总是从 `initial_lr` 重新计算**，而不是在上一step的 lr 上增量修改。这使调度是幂等的——从任何 checkpoint 恢复、重复调用同一 step，得到的 lr 都一样，断点续训不会让曲线漂移。

#### 4.1.3 源码精读

基类定义与抽象方法（注意它同时继承 `Config` 与 `Registrable`，且有 `metaclass=ABCMeta`）：

- [src/olmo_core/optim/scheduler.py:L32-L50](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L32-L50) — `Scheduler` 基类：`lr_field`/`initial_lr_field`/`units` 三个公共字段，以及必须由子类实现的抽象方法 `get_lr(initial_lr, current, t_max)`。
- [src/olmo_core/optim/scheduler.py:L27-L29](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L27-L29) — `SchedulerUnits` 枚举，只有 `steps` 和 `tokens` 两个取值。

`set_lr` 的完整实现：

- [src/olmo_core/optim/scheduler.py:L52-L102](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L52-L102) — 依次完成「字段检查 → 补 `initial_lr` → 按 units 读取 trainer 进度 → 调 `get_lr` → 写回（区分张量与浮点）」。L75-L78 和 L85-L88 是两条对 `max_steps`/`max_tokens` 的硬性校验：如果 Trainer 无法告知总进度，基于 step/token 的调度直接报配置错误。

字段名常量与 checkpoint 钉扎（承接 u5-l1）：

- [src/olmo_core/optim/config.py:L43-L44](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/config.py#L43-L44) — `LR_FIELD = "lr"`、`INITIAL_LR_FIELD = "initial_lr"`：调度器与优化器配置共享这两个字符串常量，避免魔法字符串。
- [src/olmo_core/optim/config.py:L82](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/config.py#L82) — `fixed_fields` 默认含 `initial_lr`：峰值学习率被钉进 checkpoint，续训换调度器时基准不丢。

训练循环中的调用点：

- [src/olmo_core/train/train_module/transformer/train_module.py:L608-L615](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/train/train_module/transformer/train_module.py#L608-L615) — `optim_step()` 中「Maybe adjust learning rate」：遍历所有参数组调用 `set_lr`，把新 LR 记录为 `LR (group i)` 指标，然后才 `self.optim.step()`。这也解释了为什么 WandB/Comet 日志里每组参数都有一条 LR 曲线。
- [src/olmo_core/train/train_module/transformer/train_module.py:L451-L463](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/train/train_module/transformer/train_module.py#L451-L463) — 行号对齐提示：`train_batch()` 中紧邻 `optim_step()` 之前新增了「按全局 loss token 数归一化损失」的逻辑（提交 #957：计数先在 DP 进程组内 all-reduce、再除以 DP world size），使本讲引用的 `optim_step` 锚点整体后移 13 行。该改动只影响损失目标、与调度器无交互，细节留到 u6-l2 展开。

#### 4.1.4 代码实践：脱离训练循环手动驱动 `set_lr`

测试文件里有一个非常好的范式：用 `Mock(spec=Trainer)` 伪造 trainer 状态，直接驱动 `set_lr`。我们照搬这个范式观察 `group` 字典的变化。

1. **实践目标**：验证 `set_lr` 确实只依赖 `trainer.global_step` / `max_steps`，并亲眼看 `group["lr"]` 与 `group["initial_lr"]` 如何被写入。
2. **操作步骤**（示例代码，参考 [src/test/optim/scheduler_test.py:L709-L763](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/test/optim/scheduler_test.py#L709-L763) 的写法）：

   ```python
   # 示例代码：set_lr_demo.py
   from unittest.mock import Mock
   import torch.optim as optim
   from olmo_core.optim import CosWithWarmup
   from olmo_core.train import Trainer

   model = torch.nn.Linear(4, 4)          # 任意小模型
   optimizer = optim.Adam(model.parameters(), lr=6e-4)
   scheduler = CosWithWarmup(warmup=2000)

   trainer = Mock(spec=Trainer)
   trainer.max_steps = 100_000
   trainer.max_tokens = None

   group = optimizer.param_groups[0]
   for step in [0, 1000, 2000, 51_000, 100_000]:
       trainer.global_step = step
       new_lr = scheduler.set_lr(group, trainer)
       print(f"step={step:>7}  lr={group['lr']:.6f}  initial_lr={group['initial_lr']}")
   ```

3. **需要观察的现象**：第一次调用后 `group` 里凭空多出 `initial_lr=6e-4`；step=1000 时 lr 恰为峰值的一半（3e-4）；step=100_000 时 lr 降到 `6e-4 × 0.1 = 6e-5`。
4. **预期结果**：输出与手算一致，说明 warmup 线性、余弦下限 `alpha_f=0.1` 生效。
5. 本实践在 CPU 上即可运行；具体数值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `trainer.max_steps` 设为 `None` 会发生什么？
**答案**：`set_lr` 在 [scheduler.py:L75-L78](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L75-L78) 抛出 `OLMoConfigurationError("'max_steps' must be known ...")`——基于 step 的调度必须知道总步数（horizon）才能确定曲线形状。

**练习 2**：为什么 `set_lr` 写回时专门区分「`lr` 是张量」的情况（[scheduler.py:L97-L100](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L97-L100)）？
**答案**：在 FSDP 等分布式包装下，参数组的 `lr` 可能被搬到了设备上成为 0 维张量；用 `fill_()` 原地更新可以避免每次 step 都新建张量、也避免 host↔device 同步开销。

**练习 3**：调度器每个 step 都重算 `get_lr(initial_lr, ...)`，而不是在上一步的 lr 上做增量。这带来什么好处？
**答案**：幂等性与可恢复性。从任意 checkpoint、任意 step 重新进入训练，只要 `initial_lr`（钉在 `fixed_fields` 里）和 `global_step` 正确，重建的 LR 曲线与不间断训练完全一致。

### 4.2 `CosWithWarmup`：warmup + 余弦衰减

#### 4.2.1 概念说明

`CosWithWarmup` 是 OLMo 系列主力调度器：前 `warmup` 步线性升温到峰值 `initial_lr`，随后按余弦曲线缓降到下限 \(\eta_{min}\)。三个字段决定了曲线形状：

- `warmup`（或 `warmup_fraction`）：热身长度，二选一——给绝对步数或给总步数的比例；
- `alpha_f`：**衰减下限系数**，默认 `0.1`，即最终学习率是峰值的 10% 而不是 0。这是一个容易踩的认知坑：余弦**并不衰到零**；
- `t_max`：可选的「手动总步数」。不填（默认 `None`）时用 trainer 传进来的 `max_steps`；填了就覆盖它——用于「训练 horizon 与调度 horizon 不一致」的场景（比如想在训练中段就完成衰减）。

为什么选余弦而不是线性？余弦先缓、中间快、结尾又缓，与训练损失的下降节奏匹配：中段多降一点加快收敛，结尾放缓避免在最优点附近震荡。数学上它就是「半周期」的余弦窗。

#### 4.2.2 核心流程

设峰值 LR 为 \(\eta\)（即 `initial_lr`），热身步数 \(W\)，总步数 \(T\)，当前步 \(t\)：

warmup 段（\(t < W\)，由共享辅助函数 `_linear_warmup` 计算）：

\[ \mathrm{lr}(t) = \mathrm{min\_lr} + (\eta - \mathrm{min\_lr}) \cdot \frac{\min(t, W)}{W} \]

余弦段（\(W \le t < T\)），其中 \(\eta_{min} = \alpha_f \cdot \eta\)：

\[ \mathrm{lr}(t) = \eta_{min} + (\eta - \eta_{min}) \cdot \frac{1 + \cos\!\left(\pi \cdot \frac{t - W}{T - W}\right)}{2} \]

边界行为（可直接从公式验证）：

- \(t = 0\)：lr = `warmup_min_lr`（默认 0）；
- \(t = W\)：两段公式在这一点连续，都等于 \(\eta\)；
- \(t = T\)：lr = \(\eta_{min} = \alpha_f \eta\)；超过 \(T\) 后恒为 \(\eta_{min}\)。

注意余弦段的时间轴被「平移缩放」过：先减去 \(W\) 再除以 \(T - W\)，即**衰减只在热身之后的区间上演完一整段余弦**。

#### 4.2.3 源码精读

- [src/olmo_core/optim/scheduler.py:L455-L467](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L455-L467) — `CosWithWarmup` 的字段：`warmup`/`warmup_steps`（弃用别名）/`warmup_fraction`、`alpha_f=0.1`、`t_max=None`、`warmup_min_lr=0.0`。类上的注册装饰器 `@Scheduler.register("cos_with_warmup")` 让它能被 YAML 的 `type` 字段选中（承接 u2-l2）。
- [src/olmo_core/optim/scheduler.py:L469-L485](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L469-L485) — `__post_init__` 校验：把弃用的 `warmup_steps` 迁移到 `warmup`（并发出 `DeprecationWarning`）；强制 `warmup` 与 `warmup_fraction` **恰好给一个**；`warmup_fraction` 必须在 [0, 1]。
- [src/olmo_core/optim/scheduler.py:L487-L506](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L487-L506) — `get_lr` 主体。L490 用自己的 `t_max` 覆盖传入值；L491 算 \(\eta_{min}\)；L493-L497 解析热身长度（比例则 `round(t_max * warmup_fraction)`）；L499-L500 热身段；L501-L502 越过总步数后恒返回 \(\eta_{min}\)；L504-L506 做平移缩放后套余弦公式——与上面两段公式逐项对应。
- [src/olmo_core/optim/scheduler.py:L836-L841](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L836-L841) — `_linear_warmup` 辅助函数，被 7 个调度器复用；`min(current, warmup)` 保证越界后钳制在峰值。
- [src/test/optim/scheduler_test.py:L69-L83](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/test/optim/scheduler_test.py#L69-L83) — `test_cos_with_warmup_scheduler`：用 `initial_lr=10, warmup=3000, alpha_f=0.2, T=10000` 断言了 4 个关键点：起点 4.0（`warmup_min_lr`）、热身中点 6.0、峰值 10.0、终点 2.0（= 10×0.2），以及余弦段中点等于手算的 `2 + 8·(1+cos(π·2000/7000))/2`。这份测试就是本讲公式的「可执行版」。

官方真实用法（OLMo-2 32B，峰值 LR 6e-4）：

- [src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py:L69-L79](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L69-L79) — `SkipStepAdamWConfig(lr=6e-4, ...)` 配 `scheduler=CosWithWarmup(warmup_steps=2000)`。注意它用的还是**弃用别名** `warmup_steps`——靠 `__post_init__` 自动迁移成 `warmup=2000` 并发 `DeprecationWarning`。读官方脚本时见到旧字段名不要困惑，机制就在上面 L469-L477。

#### 4.2.4 代码实践：复现 OLMo-2 32B 的学习率曲线

1. **实践目标**：独立画出 OLMo-2 32B 同款调度曲线（`lr=6e-4`、`warmup=2000`、`alpha_f=0.1`），并验证 4.2.2 的公式。
2. **操作步骤**（示例代码，只依赖 `olmo_core`，matplotlib 在 `pip install -e '.[all]'` 的 dev 依赖组里，见 [pyproject.toml:L44](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/pyproject.toml#L44)）：

   ```python
   # 示例代码：plot_cos_lr.py
   import math
   from olmo_core.optim import CosWithWarmup

   lr_peak, warmup, t_max = 6e-4, 2000, 100_000
   sched = CosWithWarmup(warmup=warmup)  # alpha_f 默认 0.1

   # 官方脚本同款：用弃用别名构造，观察 DeprecationWarning
   sched_official = CosWithWarmup(warmup_steps=2000)
   print("迁移结果:", sched_official.warmup)  # 应打印 2000

   for t in [0, 500, 1000, 2000, 25_500, 51_000, 76_500, 100_000]:
       print(f"step {t:>7}  lr = {sched.get_lr(lr_peak, t, t_max):.6e}")

   # 手算对照（热身中点、余弦中点、终点）
   assert sched.get_lr(lr_peak, 1000, t_max) == lr_peak * 1000 / warmup
   mid = lr_peak * 0.1 + lr_peak * 0.9 * (1 + math.cos(math.pi * 0.5)) / 2
   assert abs(sched.get_lr(lr_peak, (warmup + t_max) // 2, t_max) - mid) < 1e-12
   assert sched.get_lr(lr_peak, t_max, t_max) == lr_peak * 0.1
   ```

   有 matplotlib 的话再补一行 `plt.plot(range(0, t_max, 100), [sched.get_lr(lr_peak, t, t_max) for t in range(0, t_max, 100)])`；没有也没关系，上面的数值表就是曲线的采样。
3. **需要观察的现象**：lr 从 0 线性升到 6e-4（step 2000 达峰），随后对称地缓降，终点是 6e-5（不是 0）；构造 `warmup_steps=2000` 时 Python 发出一条 `DeprecationWarning`。
4. **预期结果**：断言全部通过；step 1000 处 lr=3e-4；余弦中点（step 51000）lr = \(6\cdot10^{-4}\cdot(0.1 + 0.9\times 0.5)\) = 3.3e-4。
5. 数值「待本地验证」（公式与测试断言一一对应，理论上必然通过）。

#### 4.2.5 小练习与答案

**练习 1**：`alpha_f=0.1` 时曲线终点是多少？想让训练最后 LR 趋近 0 该怎么配？
**答案**：终点是 \(\eta \times 0.1\)。设 `alpha_f=0.0` 即可衰到 0——OLMo-3 的 midtrain/long-context 阶段正是这么做的（见 4.3.3）。

**练习 2**：`warmup` 和 `warmup_fraction` 同时给、或都不给，分别会发生什么？
**答案**：都会在 `__post_init__` 抛 `OLMoConfigurationError`（[scheduler.py:L479-L480](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L479-L480) 的 `(None == None)` 恰好一个校验）。这是「二选一」字段的标准写法，WSD、PowerLR 等调度器同款。

**练习 3**：同一份 `CosWithWarmup(warmup=2000)` 配置， trainer 的 `max_steps` 从 100k 改成 200k，step 1000 处的 LR 变吗？step 100_000 处呢？
**答案**：step 1000 不变（热身段只依赖 `W` 和 \(\eta\)）；step 100_000 变——余弦段以 \(T-W\) 为分母，horizon 翻倍后该点的 \((t-W)/(T-W)\) 从 1 变成约 0.49，LR 还在高处。**余弦曲线是相对于总步数定义的**，这正是 4.3 要展开的「anneal 重设 lr」的根源。

### 4.3 调度器家族与「anneal 阶段为何重设 lr」

#### 4.3.1 概念说明

`scheduler.py` 里注册了 14 个调度器，按用途分四组：

| 组 | 注册名（`type` 字段） | 一句话说明 |
| --- | --- | --- |
| 常数/简单衰减 | `constant`、`constant_with_warmup`、`linear_with_warmup`、`inv_sqrt_with_warmup` | 峰值后保持 / 线性降 / 按 \(\sqrt{W/t}\) 降，不依赖 horizon 后段 |
| 余弦族 | `cos_with_warmup`、`half_cos_with_warmup`、`cos_with_warmup_and_linear_decay` | 完整余弦 / 余弦后半段 / 余弦截尾接线性 |
| 组合族 | `wsd`、`power_lr`、`composable`、`sequential`、`wsds` | 多段拼接：warmup-稳定-线性衰减、幂律、逐 stage 定义、子调度器串联、多周期 continual pretraining |
| 特殊 | `exponential`、`per_group` | LR range test 用的指数**上升**；按参数组分派不同子调度器 |

两个组合机制值得记住：

- **`SequentialScheduler`（串联）**：把若干子调度器按 `schedulers_max` 划分的时间段依次执行，且**前一个子调度器的终点 LR 作为下一个的 `initial_lr`**。阶段边界因此自动衔接。
- **`PerGroupScheduler`（按组）**：因为训练模块对所有参数组调用**同一个**调度器对象，单个调度器无法给不同组不同的 warmup；这个包装器读参数组上的 `scheduler_name` 标签（由 u5-l1 的 `OptimGroupOverride.opts` 打上）分派给不同子调度器——例如多模态里 connector 用 200 步 warmup、LM 用 2000 步。

#### 4.3.2 核心流程

OLMo-3 7B 的三阶段训练展示了「换阶段 = 换调度器 + 重设 lr」的完整决策链：

```
阶段 1 pretrain-1（网页级大数据 dolma3）
    lr=3e-4, CosWithWarmup(warmup=2000)，按原计划总步数 T_org 走完前半段
        ↓ 保存 checkpoint（step 596047 ≈ T_org/2）
阶段 2 pretrain-2（高质量 dolmino，即 anneal）
    载入 checkpoint（load_strategy=always, 含优化器状态）
    换调度器：HalfCosWithWarmup(warmup=T_org/2 + W_org/2)
        ↓
阶段 3 midtrain / long-context
    LinearWithWarmup(warmup=0 或 200, alpha_f=0.0)：直接线性衰到 0
```

为什么阶段 2 不能沿用 `CosWithWarmup`？因为余弦曲线**以 `t_max` 为参数**（练习 4.2-3 的结论）：

1. 若给新 horizon 上的全新 `CosWithWarmup`：曲线从头开始，LR 会重新爬回峰值 3e-4 再衰减。可阶段 1 结束时 LR 已经降到约 `eta_min + 0.9η/2`（余弦中点值），新曲线等于把 LR 猛拉上去，等于放弃退火。
2. 若沿用旧曲线不改：旧余弦相对旧 horizon 已经「演完」，LR 停在 `alpha_f·η` 不再变化，延长的训练段完全没有退火形状。
3. `HalfCosWithWarmup` 的答案：**假设配置里的峰值 LR 属于「完整的余弦」，但本次运行只从余弦的中点开始播**。代码上（见下）它把时间轴平移后 `current += t_max; t_max *= 2`，于是曲线从余弦中点值 \( \eta_{min} + (\eta - \eta_{min})/2 \) 单调降到 \( \eta_{min} \)；它自己的 `warmup` 段则把 LR 从低处线性拉到这个中点值，完成两个阶段之间的桥接。脚本注释写得很直白：`# Scheduler updated to extend lr from where we left off.`

这正是「anneal 要重设 lr」的含义：**退火阶段的目标是「在高质量数据上把 LR 沿一条单调下降的曲线送到很低」**，而曲线形状绑定了 horizon；horizon 变了，就必须重新参数化曲线（换调度器或改 `t_max`），否则要么 LR 弹回峰值、要么僵死在下限。

#### 4.3.3 源码精读

`HalfCosWithWarmup`——本讲的主角之一：

- [src/olmo_core/optim/scheduler.py:L509-L524](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L509-L524) — docstring 明说：「余弦调度的后半段，前面带一个 warmup。**假设配置的峰值 LR 是为完整余弦设定的**」。字段与 `CosWithWarmup` 相同。
- [src/olmo_core/optim/scheduler.py:L542-L564](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L542-L564) — `get_lr`：warmup 段的目标不是 \(\eta\) 而是 L555 的 `max_lr = eta_min + (initial_lr - eta_min) / 2`（余弦中点值）；余弦段在 L560-L563 先平移（减 warmup），再 `current += t_max; t_max *= 2`——等价于在一条**两倍长**的余弦上取后半段，起点恰为中点值、终点为 \(\eta_{min}\)。

`SequentialScheduler` 的衔接机制：

- [src/olmo_core/optim/scheduler.py:L913-L931](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L913-L931) — `_sequential_lr`：依次检查当前进度落在哪个子调度器的时间段；跨过某段时，L925 用该子调度器在自己 horizon 终点的 LR 作为下一段的 `initial_lr`，并把 `current`/`t_max` 都平移过去——阶段边界自动无缝。
- [src/olmo_core/optim/scheduler.py:L1140-L1193](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L1140-L1193) — `PerGroupScheduler.set_lr` 直接把整个 `group` 转交给按 `scheduler_name` 选出的子调度器（L1192-L1193），其 docstring 给了与 `OptimGroupOverride` 配合的完整示例，呼应 u5-l1。

三个官方脚本的调度器对照（均可在源码中逐行核对）：

- [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py:L72-L80](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L72-L80) — 阶段 1：`lr=LR`（[L42-L43](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L40-L43) 定义 `LR = 3e-4`、`GLOBAL_BATCH_SIZE = 8192 * 512`），`scheduler=CosWithWarmup(warmup_steps=2000)`。
- [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py:L70-L87](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py#L70-L87) — 阶段 2（anneal）：L71-L72 从第一段的常数复原出 `original_warmup_steps = 2000`、`original_max_steps = int(5e12) // GLOBAL_BATCH_SIZE`；L85-L87 换成 `HalfCosWithWarmup(warmup_steps=original_max_steps // 2 + original_warmup_steps // 2)`，注释明言是为了「extend lr from where we left off」。
- [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py:L100-L112](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py#L100-L112) — 同一脚本的 TrainerConfig：`load_path` 指向阶段 1 的 `step596047` checkpoint，`load_strategy=always` 且 `load_optim_state=True`（优化器动量一并恢复），`max_duration` 从 5T token 扩到 7T（L110）。
- [src/scripts/official/OLMo3/OLMo-3-1025-7B-midtrain.py:L72-L80](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo3/OLMo-3-1025-7B-midtrain.py#L72-L80) — 阶段 3：`LinearWithWarmup(warmup=0, alpha_f=0.0)`——不热身、线性衰到 0，是退火终点最彻底的形态（long-context 阶段则用 `warmup=200` 先短热身再衰到 0，见 [OLMo-3-1025-7B-long-context.py:L81](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py#L81)）。

一个漂亮的数字巧合可以帮你确认理解：`original_max_steps = int(5e12) // (8192 * 512) = 1_192_092`，其一半是 `596_046`，而阶段 2 载入的正是 `step596047`——**阶段 1 恰好在原计划余弦的中点收工**，与 `HalfCosWithWarmup`「从余弦中点接管」的语义严丝合缝。

#### 4.3.4 代码实践：验证 OLMo-3 两阶段的 LR 衔接

1. **实践目标**：用代码验证「fresh `CosWithWarmup` 会把 LR 弹回峰值，而 `HalfCosWithWarmup` 从余弦中点接管」。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码：anneal_handoff.py
   from olmo_core.optim import CosWithWarmup, HalfCosWithWarmup

   LR = 3e-4
   original_warmup = 2000
   original_max_steps = int(5e12) // (8192 * 512)   # = 1_192_092
   print("原计划总步数:", original_max_steps, " 一半:", original_max_steps // 2)
   print("阶段 2 载入的 checkpoint: step596047（≈ 中点 + 1）")

   mid_lr = CosWithWarmup(warmup=original_warmup).get_lr(
       LR, original_max_steps // 2, original_max_steps)
   print(f"阶段 1 结束时（余弦中点）的 lr: {mid_lr:.3e}")   # 预期 ≈ 1.65e-4

   # 反面方案：在新 horizon 上开一条全新余弦
   bad = CosWithWarmup(warmup=original_warmup)
   print(f"若换新 CosWithWarmup，step 2001 的 lr: {bad.get_lr(LR, 2001, 2_000_000):.3e}")  # 弹回 ≈ 3e-4

   # 官方方案：从余弦后半段接管
   good = HalfCosWithWarmup(warmup=original_max_steps // 2 + original_warmup // 2)
   t_max_2 = 600_000  # 阶段 2 的示意步数
   for t in [0, good.warmup // 4, good.warmup, good.warmup + (t_max_2 - good.warmup) // 2, t_max_2]:
       print(f"step {t:>8}  HalfCos lr = {good.get_lr(LR, t, t_max_2):.3e}")
   ```

3. **需要观察的现象**：阶段 1 收官 LR ≈ \(3\times10^{-4}\times(0.1+0.9\times0.5)\) = 1.65e-4；「反面方案」在热身后直接回到 ≈ 3e-4（向上弹跳）；`HalfCosWithWarmup` 的曲线先沿长 warmup 爬向中点值 1.65e-4，再沿余弦后半段单调降到 3e-5。
4. **预期结果**：`original_max_steps` 打印 1192092；三条曲线的相对位置与 4.3.2 的分析一致。把三条曲线画在一张图上（横轴统一为 step）会看到非常直观的「弹跳 vs 接管」对比。
5. 阶段 2 的真实 `t_max` 取决于其 Trainer 的 `max_steps`（由 7T token 与数据量共同决定），示意值不影响结论；具体数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`LinearWithWarmup(warmup=0, alpha_f=0.0)`（midtrain 用）描述了一条什么曲线？
**答案**：从峰值 \(\eta\) 直线降到 0 的线段（无热身、`eta_min = 0`），即纯线性退火；适合「从 checkpoint 继续、最后一步归零」的中/后训练阶段。

**练习 2**：`SequentialScheduler` 里前一个子调度器结束时 LR 是 2e-4，后一个 `CosWithWarmup(warmup=0, alpha_f=0.1)` 的 `initial_lr` 是多少？
**答案**：2e-4。见 [scheduler.py:L925](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L925)：前段终点的 `get_lr` 结果直接作为下一段的 `initial_lr` 传入，后段曲线的峰值与下限都按它缩放。

**练习 3**：你想让 embedding 参数组用 500 步 warmup、其余参数用 2000 步，单靠给 `TransformerTrainModuleConfig.scheduler` 设一个 `CosWithWarmup` 能做到吗？
**答案**：不能——训练模块对所有参数组调用同一个调度器对象。需要 `PerGroupScheduler(schedulers={"embed": CosWithWarmup(warmup=500)}, default=CosWithWarmup(warmup=2000))`，并用 u5-l1 的 `OptimGroupOverride(params=["embeddings.weight"], opts={"scheduler_name": "embed"})` 给对应组打标签（见 [scheduler.py:L1155-L1163](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/olmo_core/optim/scheduler.py#L1155-L1163) 的官方示例）。

## 5. 综合实践

**任务：为 OLMo-3 7B 绘制「三阶段学习率全景图」。**

把本讲三块知识串成一份脚本（示例代码）：

1. **阶段 1**：`CosWithWarmup(warmup=2000)`，`initial_lr=3e-4`，横轴 `0 → original_max_steps`（用 `int(5e12) // (8192*512)` 计算），标注出 step 596047 处的 LR（应 ≈ 1.65e-4）。
2. **阶段 2**：`HalfCosWithWarmup(warmup=original_max_steps//2 + 1000)`，横轴从 step 596047 起继续画（`t_max` 取阶段 2 的示意步数），检查两段曲线在衔接点的数值关系，并解释为什么官方选择「先沿长 warmup 爬坡再走余弦后半段」而不是让曲线硬跳。
3. **阶段 3**：`LinearWithWarmup(warmup=0, alpha_f=0.0)`，横轴为该阶段的全部步数，确认终点为 0。
4. 把三条曲线画进同一张图（或打印对齐的数值表），用注释标出每个阶段用的数据类型（dolma3 → dolmino → longmino，见 [src/scripts/official/OLMo3/README.md](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/scripts/official/OLMo3/README.md)）。
5. 最后运行 `pytest -v src/test/optim/scheduler_test.py`，确认本讲引用的所有公式都有测试守护（全部应通过，CPU 即可，无 GPU 标记）。

**验收标准**：你能指着自己的图回答三个问题——warmup 段斜率由什么决定（`W` 与 \(\eta\)）；余弦段为什么不会降到 0（`alpha_f` 下限）；anneal 阶段为什么必须换调度器（曲线绑 horizon，不换则弹回峰值或僵死下限）。

## 6. 本讲小结

- OLMo-core 的调度器是「Config + Registrable + 纯函数」的自研体系：`get_lr(initial_lr, current, t_max)` 只依赖峰值、当前进度与总进度，可在训练外单独计算与测试。
- `set_lr()` 是唯一的副作用出口：由 `TransformerTrainModule.optim_step()` 在每个优化步、每个参数组上调用，写回 `group["lr"]`；`initial_lr` 由 u5-l1 的构建流程钉进 `fixed_fields`，保证幂等与断点续训一致。
- `CosWithWarmup = 线性 warmup + 到 `alpha_f·η`（默认 10%）的余弦衰减；`warmup`/`warmup_fraction` 二选一，`t_max` 字段可覆盖 trainer 的 horizon；官方脚本里的 `warmup_steps=2000` 是弃用别名，靠 `__post_init__` 自动迁移。
- 余弦曲线是**相对总步数**定义的：horizon 一变，同一步的 LR 就变——这是分阶段训练必须重新设计调度的根源。
- OLMo-3 的阶段交接给了标准答案：阶段 1 恰好在原计划余弦的中点停（step 596047 ≈ T_org/2），阶段 2 用 `HalfCosWithWarmup`（峰值 LR 按「完整余弦」解释、从后半段接管）并载入优化器状态，阶段 3 用 `LinearWithWarmup(alpha_f=0.0)` 干脆衰到 0。
- 组合型调度器覆盖更复杂需求：`SequentialScheduler` 让前段终点 LR 自动成为后段 `initial_lr`；`PerGroupScheduler` 配合 `OptimGroupOverride` 给不同参数组分派不同曲线。

## 7. 下一步学习建议

- **下一讲 u5-l3（SkipStepOptimizer 与梯度调试）**：本讲已在 `optim_step` 源码里瞥见 `grad_norm` 与 `step skipped` 指标——当 loss 尖峰出现时跳过优化步，与调度器共同守护训练稳定性。
- **u6-l1（Trainer 训练循环）**：`set_lr` 依赖的 `trainer.global_step` / `max_steps` / `max_tokens` 从哪里来？答案在 `Duration`（按 step/token/epoch 停训）与 Trainer 的进度管理。
- **延伸阅读源码**：`scheduler.py` 里本讲未展开的 `WSD`、`PowerLR`、`ComposableScheduler`、`WSDS` 各有独立测试，读测试是最快的理解路径（[src/test/optim/scheduler_test.py](https://github.com/allenai/OLMo-core/blob/29ab5b92b94322dbd6e28e7d39af5068b89a23dc/src/test/optim/scheduler_test.py)）。
