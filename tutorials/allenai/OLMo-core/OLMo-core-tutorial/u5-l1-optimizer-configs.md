# 优化器配置体系：AdamW、Lion 与参数组覆盖

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `OptimConfig` 家族的公共字段（`group_overrides`、`compile`、`fixed_fields`、`mup`、`completedp`）各自的作用。
2. 解释 `build()` 如何把一个配置对象变成真实的 `torch.optim.Optimizer` 实例，包括参数组的组装顺序。
3. 用 `OptimGroupOverride` + 通配符把 embedding、lm_head 等特定参数拉出来，放进拥有独立超参的参数组。
4. 了解本轮新增的 `OptimConfig.mup` / `OptimConfig.completedp` 两个可选字段的入口位置和基本语义（深入实现留给 u10-l7 / u10-l8）。

## 2. 前置知识

### 2.1 优化器在训练循环中的位置

回顾 u1-l4：训练的每个 step 是「前向算 loss → 反向算梯度 → 优化器更新参数 → 清梯度」。优化器负责第三步：拿每个参数的梯度 \( g \)，算出一个更新量加到参数 \( p \) 上。最朴素的更新是 \( p \leftarrow p - \text{lr} \cdot g \)，其中 lr（learning rate，学习率）控制步长。

### 2.2 AdamW 与 Lion 一分钟速览

**AdamW** 为每个参数维护两个指数滑动平均：一阶矩 \( m_t \)（梯度的动量）和二阶矩 \( v_t \)（梯度平方的均值），再做偏差修正后更新：

\[
m_t = \beta_1 m_{t-1} + (1-\beta_1) g_t, \qquad
v_t = \beta_2 v_{t-1} + (1-\beta_2) g_t^2
\]

\[
p \leftarrow p\,(1 - \text{lr} \cdot \text{wd}), \qquad
p \leftarrow p - \frac{\text{lr}}{1-\beta_1^{t}} \cdot
\frac{m_t / (1-\beta_1^{t})}{\sqrt{v_t / (1-\beta_2^{t})} + \epsilon}
\]

「解耦权重衰减」（decoupled weight decay）指第一行的 \( p(1-\text{lr}\cdot\text{wd}) \) 直接缩小参数本身，而不是把 wd 加进梯度——这正是 AdamW 与原始 Adam 的区别。

**Lion**（EvoLved Sign Momentum）更省内存：它只维护一个动量，更新量取符号：

\[
u_t = \text{sign}\big(\beta_1 m_t + (1-\beta_1) g_t\big), \qquad
p \leftarrow p(1 - \text{lr}\cdot\text{wd}) - \text{lr} \cdot u_t
\]

因为更新量只有 ±1 和 0，Lion 通常要用比 AdamW 更小的 lr（本库默认 1e-4 对 1e-3），且只需一份动量状态、内存开销约为 AdamW 的一半。

### 2.3 PyTorch 的 param_groups 机制

`torch.optim.Optimizer` 接受的不是一个参数大列表，而是若干**参数组**（param groups），每组形如 `{"params": [...], "lr": ..., "weight_decay": ...}`。组内的超参覆盖构造时传入的默认值。这就允许「embedding 不做权重衰减、其余矩阵正常衰减」这类常见做法——本讲的 `OptimGroupOverride` 就是对这一机制的封装。

### 2.4 与前面讲义的衔接

u2-l2 讲过 `Registrable`：`OptimConfig` 是一个可注册基类，`@OptimConfig.register("adamw")` 把子类登记进注册表，于是一个 `optim: OptimConfig` 类型的字段（比如 `TransformerTrainModuleConfig.optim`）可以在 YAML 里靠 `type: adamw` 任意换实现。本讲就看这个家族内部长什么样。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/optim/\_\_init\_\_.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/__init__.py) | 包门面：再导出全部优化器 Config、调度器与 `MuPConfig`/`CompletedPOptimConfig` |
| [src/olmo_core/optim/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py) | 本讲主战场：`OptimConfig` 基类、`OptimGroupOverride`、`MatrixAwareOptimConfig` |
| [src/olmo_core/optim/adamw.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py) | `AdamWConfig`（包装 `torch.optim.AdamW`）与 `SkipStepAdamW(Config)` |
| [src/olmo_core/optim/lion.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/lion.py) | Lion 优化器本体与 `LionConfig`/`SkipStepLionConfig` |
| [src/olmo_core/optim/mup.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/mup.py) | muP 学习率（`MuPConfig`），本讲只看入口 |
| [src/olmo_core/optim/completedp.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/completedp.py) | CompletedP 超参缩放（`CompletedPOptimConfig`），本讲只看入口 |
| [src/test/optim/adamw_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/adamw_test.py) | 本讲实践的重要依据：参数组覆盖与 SkipStepAdamW 等价性测试 |

调用方视角：训练模块在构造时调用 `optim.build(self.model, strict=True)` 生成优化器，见 [src/olmo_core/train/train_module/transformer/train_module.py:L235-L242](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L235-L242)（`eval_only=True` 时跳过构建）。

## 4. 核心概念与源码讲解

### 4.1 OptimConfig 基类：优化器配置的公共契约

#### 4.1.1 概念说明

`OptimConfig` 是所有优化器配置的基类，继承 `Config + Registrable + Generic[Opt]`：

- `Config`：免费获得序列化与 dotlist 覆盖能力（u2-l1）。
- `Registrable`：子类靠 `@OptimConfig.register("名字")` 登记，多态字段凭 `type` 切换（u2-l2）。
- `Generic[Opt]`：泛型参数 `Opt` 约束「这个 Config 构建出来的是什么优化器类」，比如 `AdamWConfig(OptimConfig[torch.optim.AdamW])`。

它定义了**所有**优化器配置共享的五个字段——无论你选 AdamW 还是 Lion，这五个字段都在：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `group_overrides` | `Optional[List[OptimGroupOverride]]` | 把匹配通配符的参数拉进独立参数组并覆盖超参（4.3 节） |
| `compile` | `bool` | 用 `torch.compile` 编译 `optim.step()`（beta 功能） |
| `fixed_fields` | `Tuple[str, ...]` | 加载 checkpoint 后不允许被旧状态覆盖的字段，默认 `(initial_lr,)` |
| `mup` | `Optional[MuPConfig]` | 按 muP 规则给每个参数缩放 lr（本轮新增，见 u10-l7） |
| `completedp` | `Optional[CompletedPOptimConfig]` | 按 CompletedP 规则设置每组 lr/eps/wd/betas（本轮新增，见 u10-l8） |

另有一个类级标记 `_supports_completedp: ClassVar[bool] = False`：只有 AdamW 系（`AdamWConfig`、`SkipStepAdamWConfig`）把它置为 `True`。

#### 4.1.2 核心流程

`build(model)` 默认实现的流程：

```text
build(model):
    1. kwargs = self.as_dict()，弹掉 5 个库管理字段
       （group_overrides / compile / fixed_fields / mup / completedp）
    2. groups = build_groups(model)        # 4.3 节详讲
    3. optim = self.optimizer()(*groups, **kwargs)   # 真正实例化 torch 优化器
    4. 逐组设置 lr；若 compile=True 则把 lr 包成 tensor
       并给每组 setdefault("initial_lr", lr)
    5. 记录 fixed_fields 每组的当前值（快照）
    6. 打日志：几组、每组几个参数、每组超参
    7. 若 compile=True：optim.step = torch.compile(optim.step)
    8. 注册 load_state_dict 后置钩子：加载完 checkpoint 后
       用第 5 步的快照恢复 fixed_fields
    return optim
```

为什么需要第 4~8 步？因为训练中调度器会不停改写 `group["lr"]`（u5-l2），而断点续训时加载的优化器状态里带着**旧运行**的 lr。`initial_lr` 作为「本轮配置的基准 lr」被钉死在 `fixed_fields` 里，checkpoint 加载后由钩子恢复，防止上一次实验的调度进度污染新一轮 warmup。

#### 4.1.3 源码精读

类定义与公共字段（含本轮新增的 `mup`、`completedp`）：

- [src/olmo_core/optim/config.py:L60-L101](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L60-L101)：`OptimConfig` 基类声明。L66 的 `group_overrides`、L71 的 `compile`、L82 的 `fixed_fields` 是老字段；L88 的 `mup` 与 L94 的 `completedp` 是本轮 #955 新增的可选字段，默认 `None` 即完全不启用，L101 的 `_supports_completedp` 是类级能力标记。
- [src/olmo_core/optim/config.py:L43-L44](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L43-L44)：两个常量 `LR_FIELD = "lr"`、`INITIAL_LR_FIELD = "initial_lr"`，把字段名统一定义，避免魔法字符串散落各处。

抽象工厂方法与默认 `build()`：

- [src/olmo_core/optim/config.py:L208-L214](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L208-L214)：`optimizer()` 是抽象 classmethod，每个子类返回自己对应的优化器类（如 `torch.optim.AdamW`）。这是典型的「工厂方法」：基类管流程，子类只提供类。
- [src/olmo_core/optim/config.py:L216-L233](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L216-L233)：`build()` 开头先把 5 个库管理字段从 kwargs 里弹掉（L224-L229），剩下的恰好是子类自定义的超参字段（lr、betas……），原样透传给优化器构造函数（L231-L233）。注意这个设计：**子类新增的任何普通字段都会自动成为优化器构造参数**。

`initial_lr` 与 fixed_fields 钩子：

- [src/olmo_core/optim/config.py:L235-L254](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L235-L254)：逐组写 `lr`（优先取组内已有值，比如 group override 设置的 lr；否则取配置的 lr），`setdefault("initial_lr", lr)`，并把 `fixed_fields` 里出现的字段值快照下来。
- [src/olmo_core/optim/config.py:L270-L279](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L270-L279)：`compile=True` 时用 `torch.compile` 包住 `optim.step`；随后注册 `reset_fixed_fields` 后置钩子，保证加载 checkpoint 后 `initial_lr` 等字段回到本轮配置值。对应测试断言见 [src/test/optim/adamw_test.py:L79-L84](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/adamw_test.py#L79-L84)：先把 `initial_lr` 篡改为 1e-8，存取 checkpoint 后必须恢复为 `config.lr`。

构建日志（调试参数组的第一手工具）：

- [src/olmo_core/optim/config.py:L256-L268](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L256-L268)：build 完成后打印「Building AdamW optimizer with N param group(s)」，并列出每组的超参与参数个数。做本讲实践时开 INFO 级日志就能直接看到分组结果。

#### 4.1.4 代码实践

**实践目标**：验证「Config 字段 → 优化器构造参数」的透传关系，以及 `initial_lr` 的存在性。

**操作步骤**（示例代码，可保存为脚本用 `python -I script.py` 运行；CPU 即可）：

```python
# 示例代码
import logging
import torch
import torch.nn as nn

from olmo_core.optim import AdamWConfig

logging.basicConfig(level=logging.INFO)  # 打开 INFO 日志，观察 build 日志输出

model = nn.Sequential(nn.Linear(4, 8), nn.Linear(8, 4))
optim = AdamWConfig(lr=1e-3).build(model)

print(type(optim))                    # 期望 <class 'torch.optim.adamw.AdamW'>
print(len(optim.param_groups))        # 期望 1
g = optim.param_groups[0]
print(g["lr"], g["initial_lr"])       # 期望 0.001 0.001
print(g["weight_decay"], g["betas"], g["eps"])  # 期望 0.01 (0.9, 0.999) 1e-08
```

**需要观察的现象**：日志里出现「Building AdamW optimizer with 1 param group(s)...」；`param_groups[0]` 同时含 `lr` 与 `initial_lr`。

**预期结果**：与 [src/test/optim/adamw_test.py:L33-L42](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/adamw_test.py#L33-L42) 和 L64-L71 的断言一致（单组、`initial_lr` 存在）。若日志级别没开，打印部分仍应成立；完整输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`AdamWConfig(lr=1e-3).merge(["lr=0.1"]).lr` 的值是多少？这依赖 `AdamWConfig` 自己实现 `merge` 吗？

答案：`0.1`。不需要。`merge` 是 `Config` 基类的方法（u2-l1/u2-l3），`AdamWConfig` 只是继承；测试 [src/test/optim/adamw_test.py:L42](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/adamw_test.py#L42) 正是这么断言的。

**练习 2**：为什么 `fixed_fields` 默认只保护 `initial_lr` 而不保护 `lr`？

答案：`lr` 是调度器每一步都要改写的「当前值」，必须允许变化；`initial_lr` 是「本轮配置的基准」，调度器靠它计算相对比例（u5-l2），续训时不该被上一轮 checkpoint 里的值覆盖。

**练习 3**：`build()` 为什么要把 `mup`、`completedp` 从 kwargs 里弹掉而不是传给优化器？

答案：这两个字段不是任何 `torch.optim.Optimizer` 的构造参数，它们是**库级指令**——在 `build_groups` 阶段被消费（用来切分参数组），优化器本体根本不需要知道它们的存在。

### 4.2 AdamWConfig 与优化器大家族

#### 4.2.1 概念说明

`optim/` 包里注册了 9 个优化器配置，全部遵循同一模式：`@OptimConfig.register("名字")` + `@dataclass` + 一个返回优化器类的 `optimizer()` 方法。值得注意的设计取舍：

- **AdamWConfig 直接复用 `torch.optim.AdamW`**，一行 `optimizer()` 就接上了 PyTorch 官方实现；
- **Lion 是库内自实现**（PyTorch 没有 Lion），所以 lion.py 里有完整的 `Lion` 类与 `lion_step` 函数；
- **skip_step 变体**（`SkipStepAdamW`、`SkipStepLion`）在原优化器外再包一层 loss spike 检测，细节在 u5-l3；
- **矩阵感知家族**（Muon/Dion）走另一条基类 `MatrixAwareOptimConfig`，按参数维度分类而不是按通配符分组。

| 注册名 | Config 类 | 构建产物 | 定义位置 | 一句话说明 |
| --- | --- | --- | --- | --- |
| `adam` | `AdamConfig` | `torch.optim.Adam` | [adam.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adam.py) | 原始 Adam（wd 加进梯度） |
| `adamw` | `AdamWConfig` | `torch.optim.AdamW` | adamw.py | 解耦权重衰减，最常用默认选项 |
| `skip_step_adamw` | `SkipStepAdamWConfig` | `SkipStepAdamW` | adamw.py | AdamW + loss spike 跳步 |
| `lion` | `LionConfig` | `Lion` | lion.py | 符号更新，单动量省内存 |
| `skip_step_lion` | `SkipStepLionConfig` | `SkipStepLion` | lion.py | Lion + 跳步 |
| `muon` | `MuonConfig` | `Muon` | muon.py | 矩阵参数正交化更新 |
| `nor_muon` | `NorMuonConfig` | `NorMuon` | muon.py | Muon 变体：神经元级自适应 lr |
| `dion` | `DionConfig` | `Dion` | dion.py | Muon 类，支持 DP/TP 分片模型 |
| `noop` | `NoOpConfig` | `NoOpOptimizer` | noop.py | 不更新参数，只跑统计 |

（注册名均来自各文件里的 `@OptimConfig.register(...)` 装饰器；另有 `OLMoDDPOptimizerConfig` 在 moe_optimizer.py 中导出但未注册进该表。）

#### 4.2.2 核心流程

`AdamWConfig.build(model)` 的完整链路（沿用 4.1.2 的默认 `build`）：

```text
AdamWConfig(lr, betas, eps, weight_decay, foreach, fused,
            group_overrides, compile, fixed_fields, mup, completedp)
    │
    ├── optimizer() 返回 torch.optim.AdamW
    ├── build_groups(model) → List[{"params": [...], ...opts}]
    └── torch.optim.AdamW(groups, lr=..., betas=..., eps=...,
                           weight_decay=..., foreach=..., fused=...)
```

而 `SkipStepAdamW` 的更新内核 `adamw_step` 把 4.2 前置知识里的公式逐行落地，并多了一个 `step_factor`（0 表示本步被跳过，1 表示正常更新）——它把「跳步」实现为**对所有更新统一乘一个系数**，而不是真的跳过代码。

Lion 的更新则没有二阶矩：动量插值 → 取符号 → 乘 lr。

#### 4.2.3 源码精读

AdamWConfig——全家最短配置类：

- [src/olmo_core/optim/adamw.py:L233-L251](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L233-L251)：`@OptimConfig.register("adamw")` 注册；字段即 `torch.optim.AdamW` 的构造参数（lr/betas/eps/weight_decay/foreach/fused）；L247 `_supports_completedp: ClassVar[bool] = True` 声明自己支持 CompletedP；`optimizer()` 直接返回 `torch.optim.AdamW`。
- [src/olmo_core/optim/adamw.py:L12-L48](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L12-L48)：`adamw_step` 单参数更新函数。L29 解耦权重衰减 \( p \leftarrow p(1-\text{lr}\cdot\text{wd}) \)；L32-L34 两个动量的 EMA；L36-L37 偏差修正；L39-L45 拼出最终更新。它服务于 `SkipStepAdamW` 而非 `AdamWConfig`（后者用 PyTorch 官方实现），等价性由测试 [src/test/optim/adamw_test.py:L130-L191](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/adamw_test.py#L130-L191) 保证：三种实现（torch AdamW、foreach、非 foreach SkipStepAdamW）逐步对比参数与优化器状态。

LionConfig 与 Lion 本体：

- [src/olmo_core/optim/lion.py:L141-L154](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/lion.py#L141-L154)：`LionConfig` 注册为 `lion`，默认 `lr=1e-4`、`weight_decay=0.0`——和 AdamW 的默认（1e-3、1e-2）不同，印证了 2.2 节「Lion 需要更小学习率」的经验。
- [src/olmo_core/optim/lion.py:L12-L37](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/lion.py#L12-L37)：`lion_step`。L27 权重衰减；L30-L33 更新量 \( \text{sign}(\beta_1 m + (1-\beta_1) g) \) 乘 `-lr` 加到参数上；L36 动量 EMA。全程没有 `exp_avg_sq`，状态只有一份动量。

矩阵感知分支 `MatrixAwareOptimConfig`：

- [src/olmo_core/optim/config.py:L284-L331](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L284-L331)：`categorize_parameters` 按**参数维度**把 Transformer 参数分为 embed / matrix（2D 的 blocks 权重）/ vector（1D 的 norm 增益、bias）/ lm_head 四类，并断言无遗漏、无重复、无 3D+ 参数。
- [src/olmo_core/optim/muon.py:L99-L119](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/muon.py#L99-L119)：Muon 用这个分类生成默认分组——矩阵参数走 Muon，其余三组打上 `algorithm="adamw"` 标记走内部 AdamW，embedding 组顺带 `weight_decay=0.0`。
- [src/olmo_core/optim/config.py:L357-L361](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L357-L361)：矩阵感知家族**不支持**手动 `group_overrides`（直接抛 `RuntimeError`），分组完全由 `default_group_overrides` 接管。
- [src/olmo_core/optim/config.py:L394-L401](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L394-L401)：矩阵感知家族的 `build()` 明确拒绝 `mup` 与 `completedp`（配置了或模型带 CompletedP 都会抛 `OLMoConfigurationError`）——这两个新特性只支持标准逐元素优化器。

官方示例中的真实用法：

- [src/examples/llm/train.py:L181-L186](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/llm/train.py#L181-L186)：`TransformerTrainModuleConfig` 里 `optim=AdamWConfig(lr=1e-3, group_overrides=[OptimGroupOverride(params=["embeddings.weight"], opts=dict(weight_decay=0.0))])`——embedding 不衰减的教科书配置。

#### 4.2.4 代码实践

**实践目标**：对比 AdamW 与 Lion 的默认超参，并验证 SkipStepAdamW 与 torch AdamW 的等价性断言。

**操作步骤**（示例代码）：

```python
# 示例代码
from olmo_core.optim import AdamWConfig, LionConfig

aw = AdamWConfig()
li = LionConfig()
print("AdamW:", aw.lr, aw.weight_decay)   # 期望 0.001 0.01
print("Lion: ", li.lr, li.weight_decay)   # 期望 0.0001 0.0

# dotlist 覆盖（u2-l3 的机制作用在优化器配置上）
aw2 = aw.merge(["lr=0.1", "weight_decay=0.0"])
print(aw2.lr, aw2.weight_decay)           # 期望 0.1 0.0
```

然后阅读测试 [src/test/optim/adamw_test.py:L130-L191](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/adamw_test.py#L130-L191)，找到它同时对比了哪些东西（参数张量、`exp_avg`/`exp_avg_sq`/`step` 状态），并运行：

```bash
pytest -v src/test/optim/adamw_test.py -k 'equivalence or config_to_optim'
```

**需要观察的现象**：两族默认超参差异；测试全部通过（无 GPU 时 parametrize 的 cuda 设备条目按 u1-l2 讲的机制跳过）。

**预期结果**：打印值与源码默认值一致；`test_adamw_equivalence` 在 CPU 设备上通过。完整运行输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AdamWConfig` 不自己写更新公式，`SkipStepAdamWConfig` 却要写？

答案：`torch.optim.AdamW` 已是成熟实现，直接 `optimizer()` 返回它即可；而跳步需要在更新公式里插入 `step_factor` 系数（半跳步、缩放更新），官方实现做不到，于是 `SkipStepAdamW` 自带 `adamw_step`/`foreach_adamw_step`，并用等价性测试证明它与官方实现一致。

**练习 2**：若想给 `AdamWConfig` 加一个新超参字段（比如 `amsgrad`），需要改几处？

答案：只需在 dataclass 里加字段 `amsgrad: bool = False`。`build()` 的 `as_dict()` 会自动带上它传给 `torch.optim.AdamW`（其构造函数支持该参数）。这正是「子类字段自动透传」设计的收益。

**练习 3**：Muon 为什么不给 embedding 用正交化更新？

答案：embedding 是查表矩阵，每次只有被查到的行拿到梯度，正交化（对整个 2D 矩阵做 SVD 类操作）对这种稀疏更新的语义并不合适；所以 [muon.py:L110-L117](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/muon.py#L110-L117) 把 embed/vector/lm_head 三组都标记为内部 AdamW。

### 4.3 OptimGroupOverride：参数组覆盖

#### 4.3.1 概念说明

`OptimGroupOverride` 是一个只有两个字段的小 Config：

- `params: List[str]`——**全限定参数名**（FQN，即 `model.named_parameters()` 给出的名字，如 `embeddings.weight`、`blocks.0.attn_out_proj.weight`）或**通配符模式**（如 `lm_head.*`、`*.norm.weight`，用 Unix shell 风格的 fnmatch 规则匹配）；
- `opts: Dict[str, Any]`——写进对应参数组的选项，如 `dict(weight_decay=0.0)` 或 `dict(lr=2e-3)`。

它解决的问题是：**同一优化器里不同参数需要不同超参**。最常见的是「embedding / norm 增益 / bias 不做权重衰减，矩阵正常衰减」——把它们拉进单独的组，比写多个优化器简单得多。

#### 4.3.2 核心流程

`build_groups(model)` 的分组流水线：

```text
build_groups(model, strict=True):
    1. 收集 model.named_parameters()：
       requires_grad=True → all_params（有序字典）
       requires_grad=False → frozen_params（不参与优化）
    2. 对每个 OptimGroupOverride 调 _expand_param_globs：
       用 fnmatch 把通配符展开成具体参数名列表
       - 匹配 0 个：若命中冻结参数 → 警告后忽略；
         否则 strict=True 抛 OLMoConfigurationError，strict=False 仅警告
    3. 未被任何 override 覆盖的参数组成一个「默认组」（opts 为空）
       追加到组列表末尾
    4. _apply_completedp(group_overrides, model)   # 若配置了 completedp
    5. _apply_mup(group_overrides, model)          # 若配置了 mup
       （这两步会按 lr 乘数进一步切组，见 u10-l7/u10-l8）
    6. 返回 [{"params": [...], **opts}, ...]（丢弃空组）
```

组的顺序 = `group_overrides` 列表顺序 + 默认组垫底。每个参数恰好属于一组（默认组按「未被覆盖」筛出）。

#### 4.3.3 源码精读

- [src/olmo_core/optim/config.py:L47-L57](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L47-L57)：`OptimGroupOverride` 本体，两个字段各带 docstring 说明 FQN/通配符语义。
- [src/olmo_core/optim/config.py:L138-L168](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L138-L168)：`_expand_param_globs` 通配符展开。L150 用 `fnmatch(name, pattern)` 逐名匹配；L154-L160 模式只命中**冻结参数**时给出专门警告（提示你为什么「匹配不到」）；L162-L166 匹配不到任何参数时，`strict=True` 抛 `OLMoConfigurationError`——这是防止「以为覆盖了其实拼错名字」的安全网。
- [src/olmo_core/optim/config.py:L170-L206](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L170-L206)：`build_groups` 主体。L182-L186 按 `requires_grad` 把参数分成可训练/冻结两堆；L188-L191 展开所有 override；L193-L198 把没被覆盖的参数打包成默认组追加在最后；L199-L200 依次过 CompletedP 和 muP 切组（本轮新增的两个钩子，普通训练两者都是 `None`、原样返回）；L202-L206 产出最终的组字典列表，空组被过滤。
- [src/olmo_core/optim/config.py:L123-L136](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/config.py#L123-L136)：`_apply_mup` 的入口检查——muP 要求配置有 `lr` 字段，否则抛错；实际切组逻辑 `split_groups_by_lr_multiplier` 在 mup.py，属于 u10-l7 的内容。
- 行为基准测试：[src/test/optim/adamw_test.py:L45-L61](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/adamw_test.py#L45-L61) `test_adamw_config_to_optim_with_group_overrides`：用 `["wte.*"]` 覆盖后断言恰好 2 组、第一组 `weight_decay == 0.0` 且只含 1 个参数、第二组含其余全部参数、每组都有 `initial_lr`。本节实践的预期结果直接照抄这里。

#### 4.3.4 代码实践

**实践目标**：亲手复现测试中的参数组覆盖行为，并触发一次 strict 报错。

**操作步骤**（示例代码，CPU 即可）：

```python
# 示例代码
import torch.nn as nn
from olmo_core.exceptions import OLMoConfigurationError
from olmo_core.optim import AdamWConfig, OptimGroupOverride


class MyModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.wte = nn.Embedding(64, 8)   # 参数名 wte.weight
        self.fc = nn.Linear(8, 8)        # 参数名 fc.weight, fc.bias

model = MyModel()

optim = AdamWConfig(group_overrides=[
    OptimGroupOverride(params=["wte.*"], opts=dict(weight_decay=0.0)),
]).build(model)

print(len(optim.param_groups))                          # 期望 2
print(optim.param_groups[0]["weight_decay"])            # 期望 0.0
print(len(optim.param_groups[1]["params"]))             # 期望 2（fc.weight + fc.bias）

# 再试一个拼错的模式，观察 strict 报错：
try:
    AdamWConfig(group_overrides=[
        OptimGroupOverride(params=["wte2.*"], opts=dict(weight_decay=0.0)),
    ]).build(model)
except OLMoConfigurationError as e:
    print("caught:", e)
```

**需要观察的现象**：两组划分与测试断言一致；拼错模式抛出 `OLMoConfigurationError`，消息形如「optim group 0 override pattern 'wte2.*' does not match any parameters」。

**预期结果**：输出 `2`、`0.0`、`2` 与 caught 消息；逐字输出待本地验证。可再把 `build(model, strict=False)` 改成非严格模式，观察报错降级为日志警告、模型退化为单组。

#### 4.3.5 小练习与答案

**练习 1**：`group_overrides` 里两个模式的匹配范围有重叠（如 `["blocks.*"]` 与 `["blocks.0.*"]`）会发生什么？

答案：不会报错，但同名参数会出现在**两个**组里，PyTorch 优化器会对它应用后一个组的更新（且状态字典行为容易混乱）。`build_groups` 只保证「没被任何模式覆盖的参数进默认组」，不去重交叉覆盖，写模式时要自己避免重叠。

**练习 2**：为什么冻结参数（`requires_grad=False`）匹配到模式时只是警告而不是报错？

答案：冻结参数本来就不进优化器（L182-L186 直接排除），匹配它是无操作；这种情况常见于通配符比预期宽或复用配置冻结了部分层，警告足以提示，报错反而误伤合法场景。而「什么都没匹配到」几乎必然是名字写错，所以 strict 模式直接拦下。

**练习 3**：默认组（无 override 的那一组）的 `opts` 是什么？它的超参从哪来？

答案：空字典（L195-L197 `OptimGroupOverride([...], {})`）。组里没有任何覆盖项时，`{"params": [...], **{}}` 只剩 params，PyTorch 会用优化器构造时的默认值（即 Config 的 lr/weight_decay 等）。

## 5. 综合实践

把本讲三块内容串起来：**给一个真实的小 Transformer 配置 AdamW，并用 OptimGroupOverride 给 embedding 与 lm_head 设置不同的权重衰减，build 后打印参数组划分验证生效。**

**实践目标**：在真实模型（而非玩具 nn.Module）上走完「Config → 分组 → 优化器」全链路，并核对每组参数。

**操作步骤**（示例代码，CPU 即可）：

```python
# 示例代码
from olmo_core.nn.transformer import TransformerConfig
from olmo_core.optim import AdamWConfig, OptimGroupOverride

# 1. 用预置工厂方法构建 14M 小模型（u3-l2），lm_head 不与 embedding 绑定（默认
#    tie_word_embeddings=False，见 config.py 的 TransformerConfig 字段默认值）
model_config = TransformerConfig.olmo2_14M(vocab_size=1000)
model = model_config.build()  # 优化器分组只看参数名，无需初始化权重

# 2. 配置 AdamW：embedding 完全不衰减；lm_head 不衰减且用 2 倍学习率
optim_config = AdamWConfig(
    lr=1e-3,
    weight_decay=1e-2,
    group_overrides=[
        OptimGroupOverride(params=["embeddings.weight"], opts=dict(weight_decay=0.0)),
        OptimGroupOverride(params=["lm_head.*"], opts=dict(weight_decay=0.0, lr=2e-3)),
    ],
)

# 3. 构建并检查分组
optim = optim_config.build(model)
name_by_id = {id(p): n for n, p in model.named_parameters()}

total = 0
for i, group in enumerate(optim.param_groups):
    names = [name_by_id[id(p)] for p in group["params"]]
    total += len(names)
    print(f"Group {i}: {len(names)} params, lr={group['lr']}, "
          f"initial_lr={group['initial_lr']}, weight_decay={group['weight_decay']}")
    for n in names:
        print("   ", n)

assert total == sum(1 for _ in model.named_parameters())  # 每个参数恰好归入一组
```

**需要观察的现象**：

1. 恰好 3 个参数组，顺序 = 两个 override + 默认组垫底。
2. Group 0 只含 `embeddings.weight`，`weight_decay=0.0`，`lr=1e-3`。
3. Group 1 含 `lm_head.norm.weight` 与 `lm_head.w_out.weight`（llama_like 系预设的 LMHead 带 norm、无 bias，见 [src/olmo_core/nn/transformer/config.py:L1806](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L1806)），`weight_decay=0.0` 且 `lr=2e-3`——注意 `initial_lr` 也应为 `2e-3`（组内已有 lr 时 L239-L250 优先取组内的）。
4. Group 2 是全部 `blocks.*` 参数，`weight_decay=1e-2`、`lr=1e-3`。
5. 打开 INFO 日志可看到 4.1.3 提到的分组日志。

**预期结果**：以上 5 点全部成立，断言通过。若把 `params=["lm_head.*"]` 换成不存在的 `params=["output.*"]`，`build` 应抛 `OLMoConfigurationError`。各组的准确参数个数与逐行输出待本地验证。

**延伸**（可选）：把 `optim_config` 用 `as_config_dict()` 存成 JSON 再 `OptimConfig.from_dict` 读回来（u2-l2 的多态反序列化），确认 `type` 字段是 `adamw`、分组信息无损往返。

## 6. 本讲小结

- `OptimConfig` 是可注册（Registrable）的优化器配置基类：5 个公共字段（`group_overrides`/`compile`/`fixed_fields`/`mup`/`completedp`）由基类统一管理，子类字段自动透传为优化器构造参数。
- `build(model)` 的固定流程：弹掉库管理字段 → `build_groups` 分组 → 工厂方法实例化 → 逐组设 `lr`/`initial_lr` → 快照 `fixed_fields` 并注册 checkpoint 后置钩子 → 可选 `torch.compile`。
- 家族全景：`adamw`（直接复用 `torch.optim.AdamW`）、`lion`（库内自实现，符号更新、单动量）、skip_step 变体（u5-l3）、矩阵感知的 muon/nor_muon/dion（走 `MatrixAwareOptimConfig`，不支持手动分组，也不支持 muP/CompletedP）。
- `OptimGroupOverride` 用 fnmatch 通配符按全限定参数名拉组，未覆盖参数进默认组；strict 模式下模式匹配不到参数会直接报错，命中冻结参数只警告。
- 本轮 #955 新增的 `mup` 与 `completedp` 是挂在 `OptimConfig` 上的可选字段，在 `build_groups` 末尾按 lr 乘数进一步切组；二者仅 AdamW 系支持（`_supports_completedp`），深入实现见 u10-l7/u10-l8。
- `initial_lr` 被钉进 `fixed_fields`：调度器随时改 `lr`，但断点续训加载旧优化器状态后基准 lr 不被污染。

## 7. 下一步学习建议

下一讲 **u5-l2 学习率调度器**：`optim/scheduler.py` 里的 `CosWithWarmup` 等调度器如何每步读取参数组的 `initial_lr` 计算当前 `lr`——本讲的 `initial_lr` 伏笔在那里兑现。阅读建议：先看 `Scheduler` 基类的 `step()` 与 `SchedulerUnits`，再对比 `CosWithWarmup` 和 `WSD` 两条曲线。之后再进 u5-l3（SkipStepOptimizer 与梯度调试）。若你对 `mup`/`completedp` 两个新字段好奇，可以先读 [src/olmo_core/optim/mup.py:L40-L56](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/mup.py#L40-L56) 的 MuPConfig docstring 预热，正式讲解在 u10-l7。
