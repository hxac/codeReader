# 应用组合根 composition

> 单元 u3 · 入口与启动链路 · 第 4 讲
> 依赖：u3-l3（拓扑构建器 launch）、u4-l1（算法契约 contracts）

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清「组合根（composition root）」在 SpecForge 里的唯一落点，以及它为什么必须**只有一处**。
2. 读懂 `resolve_run` 如何用一个字符串字段 `training.strategy` 在算法注册表里查到不可变的 `AlgorithmRegistration`。
3. 读懂 `bind_run` + `validate_resolved_run` 如何在「装配」之前完成算法感知的校验，把不支持的组合拦在启动之前。
4. 读懂 `build_application_run` 如何把解析结果无副作用地交给 `training.assembly`，最终得到一个可 `.run()` 的对象。
5. 在源码里追踪 `cli._train` → `build_application_run(resolved).run()` 这条主链。

本讲只讲「装配之前」的解析与校验，**不**深入装配内部的模型构建与训练循环（那是 u6 训练主链路的内容），也**不**展开算法契约的每个字段（那是 u4-l1 的内容，本讲只承接它）。

---

## 2. 前置知识

### 2.1 什么是「组合根」

如果你给一个程序输入「一份配置」，它最终要变成「一堆正在跑的对象」（模型、优化器、数据加载器、分布式进程组）。**从配置到对象的那一次「接线」，就叫组合根。**

很多项目把接线散落在各处：CLI 里 new 一个模型、训练循环里再 new 一个优化器、数据模块里又查一次算法名。SpecForge 的设计原则相反——**整个进程里只允许有一个组合根**，就是 `specforge/application/composition.py`。这样有三个好处：

- 名字到行为的解析只发生一次，不会出现「同一个算法名在两处解析出不同结果」。
- 校验集中在装配之前，启动失败时能 fail-fast（快速失败、给出清晰报错），而不是跑到一半才崩。
- 程序化调用（不经过 CLI）和命令行调用走完全相同的接线，行为可复现。

### 2.2 承接 u4-l1 的关键认知

在 u4-l1 中你已经知道，SpecForge 把一个算法拆成「两半」塞进同一个 `AlgorithmRegistration`：

| 半边 | 类型 | 内容 | 谁读它 |
|------|------|------|--------|
| 纯契约 | `AlgorithmSpec` | 纯数据：声明需要哪些特征、支持哪些 attention 后端、草稿架构约束……**不含任何可执行对象** | 规划层（本讲 `planning.py`） |
| 可执行 | `providers` | 工厂函数：怎么建模型、怎么建 reader、怎么算一步 loss…… | 装配层（本讲的下游 `assembly.py`） |

这种「spec 只放值、providers 只放行为」的二分，是本讲组合根能做「纯值校验」的前提。如果你还不熟悉 `AlgorithmSpec` 的字段，可以快速回顾 u4-l1；本讲用到时会给一句话提醒。

### 2.3 一个字符串字段串起一切

回忆 u2-l2：`training.strategy` 在配置层（`schema.py`）里只是一个普通字符串，默认值是 `"eagle3"`：

```python
# specforge/config/schema.py:482
strategy: str = "eagle3"
```

schema 层**不校验**它是不是真实存在的算法名。真正把它翻译成「行为」的地方，就是本讲的 `resolve_run`。这正是「组合根拥有解析权」的体现。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 职责 |
|------|------|
| [specforge/application/composition.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py) | **唯一组合根**：`ResolvedRun` 值对象、`resolve_run` / `bind_run` / `build_application_run` |
| [specforge/application/planning.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py) | 算法感知校验：`validate_resolved_run` 及六个子校验器 |
| [specforge/application/__init__.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/__init__.py) | 对外暴露组合根的公共名字 |
| [specforge/algorithms/registry.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py) | `AlgorithmRegistration` / `AlgorithmRegistry`：不可变目录与按名查找 |
| [specforge/algorithms/builtin.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py) | 把全部内置算法显式装配成一个注册表 |
| [specforge/cli.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py) | 调用方：`resolve_run` 在 supervisor 侧、`bind_run` 在 worker 侧、`_train` 调 `build_application_run` |
| [specforge/training/assembly.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py) | 下游消费者：`build_training_run` 把解析结果真正变成可执行对象 |

一句话总览：**`resolve_run`（解析）→ `bind_run`（校验）→ `build_application_run`（装配）**，三步都在 `composition.py` 里，分别借助 `registry.py`、`planning.py` 和 `assembly.py`。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：组合根与值对象、`resolve_run`、`bind_run`/`validate_resolved_run`、`build_application_run`。

### 4.1 组合根与 ResolvedRun 值对象

#### 4.1.1 概念说明

组合根要解决的核心问题是：**配置是一份「被动」的数据，算法是一组「主动」的行为，如何安全地把它们绑在一起？**

SpecForge 的答案是把绑定结果做成一个**不可变的值对象** `ResolvedRun`——它同时持有「已校验的配置」和「这一个算法的注册」。一旦构造出来，它就是一个不会再变、可以被放心传递给下游的「决议结果」。

为什么强调「不可变（frozen）」？因为整个装配链路里，配置和算法注册会被传过很多层（CLI → launch → assembly → runtime）。如果中途有人能改它，就很难追踪「最终跑起来的是哪一份」。`frozen=True` 让任何试图修改字段的代码直接抛异常，从机制上杜绝了偷偷改值。

#### 4.1.2 核心流程

组合根对外暴露三件套，按调用顺序：

```
            resolve_run(cfg)                # 解析 + 校验  ①
                  │
                  ▼
            ResolvedRun(config, algorithm)  # 不可变值对象
                  │
                  ▼
     build_application_run(resolved)        # 装配  ②
                  │
                  ▼
         build_training_run(...)            # 下游 assembly.py
                  │
                  ▼
            TrainingRun().run()             # 真正开跑
```

`bind_run(cfg, algorithm)` 是 ① 的内半步：它只负责「拿一个已存在的注册 + 一个配置，跑校验后包成 `ResolvedRun`」。`resolve_run` 多做的一步是「按名字查出注册」。

#### 4.1.3 源码精读

先看值对象本身：

```python
# specforge/application/composition.py:12-17
@dataclass(frozen=True)
class ResolvedRun:
    """A validated config paired with its one algorithm registration."""

    config: Config
    algorithm: AlgorithmRegistration
```

[specforge/application/composition.py:12-17](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L12-L17) 定义了 `ResolvedRun`：`frozen=True` 保证不可变，注释里点明它本质是「一份已校验配置 + 它对应的唯一算法注册」。

同文件里还有一个并列的值对象 `ResolvedOfflineCapture`：

```python
# specforge/application/composition.py:20-28
@dataclass(frozen=True)
class ResolvedOfflineCapture:
    """Algorithm-owned schema and layer plan for local feature preparation."""

    run: ResolvedRun
    draft_config: object
    capture_method: str
    capture_layers: tuple[int, ...]
    layout: OfflineCaptureLayout
```

[specforge/application/composition.py:20-28](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L20-L28) 定义了它。这个对象不是训练主链路用的，而是给「离线特征准备」命令（`scripts/prepare_hidden_states.py`，见 u5-l3）用的——它把 `ResolvedRun` 和「要捕获哪几层、用什么 schema 持久化」打包在一起。本讲只提它的存在，说明组合根不只服务训练，也服务离线准备，**两条路共用同一个解析+校验入口**。

最后看 `__init__.py` 对外暴露了什么：

```python
# specforge/application/__init__.py:3-10
from specforge.application.composition import (
    ResolvedOfflineCapture,
    ResolvedRun,
    bind_run,
    build_application_run,
    resolve_offline_capture,
    resolve_run,
)
```

[specforge/application/__init__.py:3-10](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/__init__.py#L3-L10) 是这个包的全部对外门面。注意：**它没有暴露 `planning.validate_resolved_run`**——校验是组合根的内部实现细节，外部只该调用 `resolve_run` / `bind_run` / `build_application_run`。这是一个值得学习的封装边界。

#### 4.1.4 代码实践

**实践目标**：确认组合根只暴露三件套，校验函数不对外。

**操作步骤**：

1. 打开 `specforge/application/__init__.py`，数一数 `__all__` 里有几个名字。
2. 在一个临时 Python 解释器里尝试 `from specforge.application import validate_resolved_run`，观察是否报 `ImportError`。

**需要观察的现象**：

- `__all__` 里只有 `ResolvedOfflineCapture`、`ResolvedRun`、`bind_run`、`build_application_run`、`resolve_offline_capture`、`resolve_run` 六个名字。
- `validate_resolved_run` 无法从包顶层导入，必须写成 `from specforge.application.planning import validate_resolved_run` 才能拿到。

**预期结果**：组合根对外只给「值对象 + 解析/绑定/装配」，校验被藏在内部子模块。如果你在业务代码里写 `from specforge.application import validate_resolved_run`，这就是一个坏味道——说明你想绕过组合根自己校验，应该改成调 `resolve_run`。

#### 4.1.5 小练习与答案

**练习 1**：`ResolvedRun` 为什么用 `frozen=True`？如果去掉会出什么问题？

> **参考答案**：`frozen=True` 让 `ResolvedRun` 不可变。装配链路很长（CLI→launch→assembly→runtime），不可变保证「传到最后还是同一份决议」，任何中途赋值都会直接抛异常。去掉后，某层可能悄悄改 `config` 或 `algorithm`，导致最终跑起来的配置和当初校验过的不一致，且极难排查。

**练习 2**：为什么 `ResolvedOfflineCapture` 内部持有一个 `run: ResolvedRun`，而不是直接持 `config` 和 `algorithm`？

> **参考答案**：复用已校验的值对象。离线特征准备和训练共享同一个「解析+校验」结果，把 `ResolvedRun` 嵌进去既避免重复校验，又保证了「准备特征用的算法」和「训练用的算法」是同一个，不会错配。

---

### 4.2 resolve_run：用 training.strategy 找到 AlgorithmRegistration

#### 4.2.1 概念说明

`resolve_run` 是组合根里「解析」这半步。它要做的事用一句话讲：**把 `cfg.training.strategy` 这个字符串，翻译成内存里真实的 `AlgorithmRegistration` 对象。**

为什么需要这一步？因为配置文件里写的是 `strategy: eagle3`（一个字符串），而下游装配需要的是「这个算法的全部行为」（建模型的工厂、建 reader 的工厂、支持的 attention 后端……）。字符串和行为之间的映射表，就是**算法注册表**（u4-l2 的主题）。`resolve_run` 就是查表的那一次调用。

一个关键设计：**解析只发生一次，且只有组合根有解析权。** 下游 `assembly.py` 拿到的是已经解析好的 `AlgorithmRegistration` 对象，它**绝不**再去查名字——这点我们会在 4.4 的源码里直接看到。

#### 4.2.2 核心流程

`resolve_run` 的内部步骤：

```
resolve_run(cfg, registry=None)
   │
   ├─ registry 为空？  ──是──▶ 取默认 builtin_algorithm_registry()
   │
   ├─ algorithm = registry.resolve(cfg.training.strategy)   # 按名查表
   │       └─ 找不到 → 抛 KeyError
   │
   ├─ 把 KeyError 翻译成 ValueError（更友好的公共报错）
   │
   └─ return bind_run(cfg, algorithm)   # 进入校验（见 4.3）
```

注意三处「防御性」设计：默认注册表懒加载、异常类型转译、最终委托给 `bind_run`。

#### 4.2.3 源码精读

先看 `resolve_run` 本体：

```python
# specforge/application/composition.py:40-55
def resolve_run(
    cfg: Config,
    registry: AlgorithmRegistry | None = None,
) -> ResolvedRun:
    """Resolve and validate all algorithm-owned behavior exactly once."""

    if registry is None:
        from specforge.algorithms.builtin import builtin_algorithm_registry
        registry = builtin_algorithm_registry()
    try:
        algorithm = registry.resolve(cfg.training.strategy)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc

    return bind_run(cfg, algorithm)
```

[specforge/application/composition.py:40-55](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L40-L55) 是组合根的解析入口。三个要点：

1. **默认注册表懒加载**：`registry` 默认 `None`，此时才 `import` 并调用 `builtin_algorithm_registry()`。这种「延迟 import」让 `composition.py` 模块本身不直接依赖算法包，import 组合根不会顺带 import 全部算法。
2. **按名查表**：`registry.resolve(cfg.training.strategy)` 是唯一的解析点。
3. **异常转译**：把 `KeyError`（"找不到这个名字"）翻译成 `ValueError`。对调用方（CLI）来说，`ValueError` 更符合「配置有问题」的语义。

接着看注册表怎么查。`registry.resolve` 是线性查找：

```python
# specforge/algorithms/registry.py:78-84
def resolve(self, name: str) -> AlgorithmRegistration:
    for registration in self._registrations:
        if registration.name == name:
            return registration
    raise KeyError(
        f"unknown algorithm {name!r}; registered algorithms: {list(self.names)}"
    )
```

[specforge/algorithms/registry.py:78-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L78-L84) 是按名查找。找不到时抛 `KeyError`，且**报错信息里列出全部已注册算法名**——这正是你拼错 `strategy` 时能看到 `registered algorithms: [...]` 的原因。

再往上看一层：注册表在构造时就排过序、查过重：

```python
# specforge/algorithms/registry.py:60-68
names = [registration.name for registration in resolved]
duplicates = sorted(name for name in set(names) if names.count(name) > 1)
if duplicates:
    raise ValueError(f"duplicate algorithm registrations: {duplicates}")
object.__setattr__(
    self,
    "_registrations",
    tuple(sorted(resolved, key=lambda registration: registration.name)),
)
```

[specforge/algorithms/registry.py:60-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L60-L68) 保证注册表里名字唯一（重名直接报错），且按名字排序——这让 `resolve` 即使线性查找，行为也完全确定，不会因为注册顺序不同而解析出不同结果。

那这些注册从哪儿来？看默认注册表：

```python
# specforge/algorithms/builtin.py:13-16
def builtin_algorithm_registry() -> AlgorithmRegistry:
    """Return a fresh immutable catalog without module-level mutation."""

    return AlgorithmRegistry((eagle3(), peagle(), dflash(), domino(), dspark()))
```

[specforge/algorithms/builtin.py:13-16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py#L13-L16) 显式列出了五个内置算法（`eagle3` / `peagle` / `dflash` / `domino` / `dspark`）。注意它**每次调用都返回一个全新的不可变目录**（"without module-level mutation"）——没有全局变量，没有副作用，测试和程序化调用都能拿到干净的实例。这也是 `resolve_run` 里能反复 `builtin_algorithm_registry()` 的原因。

最后注意：`resolve_run` 的返回值不只是「注册」，而是 `bind_run(cfg, algorithm)` 的结果——也就是说**解析和校验是绑死的一对**，查到名字后立刻校验，不会出现「查到了但没校验」的中间态。

#### 4.2.4 代码实践

**实践目标**：观察 `training.strategy` 拼错时的报错信息，确认它来自 `registry.resolve`。

**操作步骤**：

1. 找一份示例配置，例如 `examples/configs/qwen3-8b-eagle3-disaggregated.yaml`（u2-l1 用过）。
2. 用 u2-l3 学过的覆盖语法，把 strategy 改成一个不存在的名字，配合 `--plan` 预览（不起训练、不占 GPU）：

   ```bash
   specforge train --config examples/configs/qwen3-8b-eagle3-disaggregated.yaml \
       --plan training.strategy=no_such_algo
   ```

   > 说明：本实践假设你已按 u1-l2 装好 `specforge`。若仓库里该示例文件名不同，替换成任意一份真实存在的 eagle3 配置即可。命令本身不占 GPU，因为它在 `--plan` 阶段就会因校验失败而退出。

**需要观察的现象**：

- 程序应当报错并退出，报错文本里出现类似 `unknown algorithm 'no_such_algo'; registered algorithms: ['domino', 'dflash', 'dspark', 'eagle3', 'peagle']` 的字样。

**预期结果**：报错链路是 `registry.resolve` 抛 `KeyError` → `resolve_run` 转成 `ValueError` → CLI 打印。这一现象证明了「strategy 字符串 → 注册」的翻译就发生在 `resolve_run` 里，且找不到名字时 fail-fast。

> ⚠️ 待本地验证：不同版本的内置算法列表可能变化，请以你本地报错里实际列出的 `registered algorithms` 为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `resolve_run` 把 `KeyError` 转成 `ValueError`？

> **参考答案**：语义不同。`KeyError` 通常表示「字典/集合里没有这个键」，偏内部；而对 CLI 调用方来说，「strategy 名字不在注册表里」本质是「配置值非法」，属于 `ValueError` 的范畴。转译后调用方只需捕获 `ValueError` 就能覆盖所有「配置/解析」类错误。

**练习 2**：如果 `resolve_run` 允许传入自定义 `registry`，有什么实际用处？

> **参考答案**：方便测试和扩展。测试时可以传入一个只含「桩（stub）算法」的最小注册表，避免 import 真实的 torch/transformers；二次开发时（u10-l2），新增算法的开发者可以构造一个只含自己算法的注册表，单独验证解析与校验逻辑，而不必先动 `builtin.py`。

---

### 4.3 bind_run 与 validate_resolved_run：算法感知校验

#### 4.3.1 概念说明

解析只是「查到名字」，但查到不代表「能用」。比如：你选了 `eagle3`，却在配置里把 attention 后端设成它不支持的值；或者你选了「在线模式」，却没有提供 disaggregated 部署——这些都是「名字对得上、但组合不合法」的情况。

`bind_run` 就是组合根里「校验」这半步：**把配置和已解析的算法放在一起，逐条比对算法契约（spec）声明的约束，任何一条不满足就 fail-fast。**

它和 `resolve_run` 的分工很清晰：

- `resolve_run` = 解析（查名字）+ 校验（调 `bind_run`）。
- `bind_run` = 只校验（假定注册已经拿到），产出 `ResolvedRun`。

为什么把 `bind_run` 单独拆出来？因为 CLI 有一处特殊场景需要它：worker 进程拿到的是**角色投影后的配置**（u3-l1 的 `_config_for_role`），注册已经在 supervisor 侧解析过了，worker 侧只需用同一份注册**重新校验角色配置**——这时调 `bind_run`（不重新解析）正好。我们在 4.4 的调用链里会看到这一点。

#### 4.3.2 核心流程

`bind_run` 极薄，真正干活的是 `validate_resolved_run`：

```python
# specforge/application/composition.py:31-37
def bind_run(cfg: Config, algorithm: AlgorithmRegistration) -> ResolvedRun:
    from specforge.application.planning import validate_resolved_run
    validate_resolved_run(cfg, algorithm)
    return ResolvedRun(config=cfg, algorithm=algorithm)
```

校验由六个子校验器串联，在 `validate_resolved_run` 里按顺序调用：

```
validate_resolved_run(cfg, algorithm)
   │
   ├─ 0. 名字一致性：algorithm.name == cfg.training.strategy
   ├─ 1. _validate_feature_provider   特征契约 (mode, modality) 是否被支持
   ├─ 2. _validate_draft_options      草稿配置/override 是否被算法允许
   ├─ 3. _validate_algorithm_capabilities  attention 后端 / batch_size / compact_teacher
   ├─ 4. _validate_training_topology  在线/离线 × 部署模式 × TP/SP 的拓扑约束
   └─ 5. _validate_vocab_mapping      disaggregated 是否要求 vocab_mapping_path
```

任何一步抛 `ValueError`，`bind_run` 都不会构造 `ResolvedRun`。

#### 4.3.3 源码精读

先看 `validate_resolved_run` 的总装：

```python
# specforge/application/planning.py:189-205
def validate_resolved_run(
    cfg: Config,
    algorithm: AlgorithmRegistration,
) -> None:
    """Validate one config against its resolved pure contract and providers."""

    if algorithm.name != cfg.training.strategy:
        raise ValueError(
            "resolved algorithm does not match training.strategy: "
            f"{algorithm.name!r} != {cfg.training.strategy!r}"
        )
    mode = _feature_mode(cfg)
    _validate_feature_provider(cfg, algorithm, mode)
    _validate_draft_options(cfg, algorithm)
    _validate_algorithm_capabilities(cfg, algorithm, mode)
    _validate_training_topology(cfg, mode)
    _validate_vocab_mapping(cfg, algorithm, mode)
```

[specforge/application/planning.py:189-205](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L189-L205) 是校验总入口。第 0 步是个**断言性校验**：确保传进来的 `algorithm` 名字和配置里的 `strategy` 一致——防止调用方把 A 算法的注册拿去配 B 算法的配置。随后五个子校验器各管一类约束。

`mode` 是怎么定的？它来自配置的数据模式（u2-l1）：

```python
# specforge/application/planning.py:10-11
def _feature_mode(cfg: Config) -> FeatureMode:
    return FeatureMode.OFFLINE if cfg.mode == "offline" else FeatureMode.STREAMING
```

[specforge/application/planning.py:10-11](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L10-L11) 把配置的 `mode`（"offline"/"online"，由 `data.hidden_states_path` 是否为空推导，见 u2-l1）映射成算法契约用的 `FeatureMode`（OFFLINE/STREAMING，u4-l1 讲过）。注意「online」对应 `STREAMING`——在线训练的特征是「流式」捕获的。

挑两个最能体现「算法感知」的校验器细看。第一个是特征契约校验：

```python
# specforge/application/planning.py:14-37
def _validate_feature_provider(cfg, algorithm, mode):
    modality = cfg.model.input_modality
    spec = algorithm.spec
    if not spec.supports(mode, modality):
        supported = sorted(...)
        raise ValueError(
            f"algorithm {algorithm.name!r} has no {mode.value} feature contract "
            f"and provider for modality {modality!r}; supported: {supported}"
        )
    if mode is FeatureMode.OFFLINE:
        algorithm.providers.offline_for(modality)
    else:
        algorithm.providers.server_streaming_for(modality)
```

[specforge/application/planning.py:14-37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L14-L37) 先问算法的纯契约 `spec.supports(mode, modality)`（u4-l1 里 `AlgorithmSpec` 的方法）——这个算法到底声明了没有？声明了再去解析对应的 provider（`offline_for` / `server_streaming_for`）作为防御性断言。这样「配置要求的 (mode, modality)」必须同时满足「契约声明」和「provider 存在」两道关。

第二个是拓扑校验，它最能体现「不支持的组合直接 fail-fast」：

```python
# specforge/application/planning.py:135-166
if mode is FeatureMode.STREAMING:
    if deployment_mode != "disaggregated":
        raise ValueError(
            "online training requires deployment.mode=disaggregated; "
            "colocated online training is no longer supported"
        )
    if cfg.model.target_backend != "sglang":
        raise ValueError(
            "online training uses an external SGLang capture server and "
            "requires model.target_backend=sglang"
        )
    ...
    if (
        cfg.training.tp_size != 1
        or cfg.training.sp_ulysses_size != 1
        or cfg.training.sp_ring_size != 1
    ):
        raise ValueError(
            "the disaggregated online consumer uses every trainer rank for "
            "data parallelism; configure target TP on the external server and "
            "keep training.tp_size/sp sizes at 1"
        )
```

[specforge/application/planning.py:135-166](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L135-L166) 把 u3-l3 里讲过的拓扑铁律逐条写成了校验：在线必须 disaggregated、必须用 sglang、在线 consumer 的 TP/SP 必须是 1（每个 trainer rank 都拿来做数据并行，目标模型的 TP 放在外部服务器上）。任何一条不满足，装配都不会发生。

还有一处容易被忽略但很重要的校验：

```python
# specforge/application/planning.py:172-186
def _validate_vocab_mapping(cfg, algorithm, mode):
    if (
        cfg.deployment.mode == "disaggregated"
        and mode in algorithm.providers.vocab_mapping_modes
        and not cfg.model.vocab_mapping_path
    ):
        raise ValueError(
            f"algorithm {algorithm.name!r} disaggregated runs require "
            "model.vocab_mapping_path because producer and consumer cannot "
            "derive one shared mapping"
        )
```

[specforge/application/planning.py:172-186](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L172-L186) 说明了一个跨进程的约束：disaggregated 模式下，producer 和 consumer 是两个进程，它们没法像单进程那样现场推导一份共享的 vocab 映射，所以算法若声明了需要 vocab_mapping（`vocab_mapping_modes` 非空），就必须显式给 `model.vocab_mapping_path`。这种「把分布式约束前置到校验」的做法，正是组合根的价值。

#### 4.3.4 代码实践

**实践目标**：用一个故意不合法的组合，观察拓扑校验如何 fail-fast。

**操作步骤**：

1. 仍然用一份在线（disaggregated）的 eagle3 示例配置。
2. 用覆盖语法，把 `model.target_backend` 改成一个非法值，配合 `--plan`：

   ```bash
   specforge train --config examples/configs/qwen3-8b-eagle3-disaggregated.yaml \
       --plan model.target_backend=hf
   ```

   > 说明：该示例是一份 online/disaggregated 配置（u2-l1）。把 target_backend 改成非 sglang 会违反 `_validate_training_topology` 的 STREAMING 分支。

**需要观察的现象**：

- 程序报错退出，文本里出现 `online training uses an external SGLang capture server and requires model.target_backend=sglang`。

**预期结果**：报错文本与 [planning.py:141-145](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L141-L145) 完全对应。这证明校验发生在装配之前、组合根之内。

> ⚠️ 待本地验证：取决于你选的示例配置字段是否允许这种覆盖（StrictConfigModel 对未知字段会报错）。若覆盖被拒，请挑选一份你确认含 `model.target_backend` 字段的配置。

#### 4.3.5 小练习与答案

**练习 1**：`bind_run` 和 `resolve_run` 都会调用 `validate_resolved_run` 吗？为什么 CLI 里 worker 进程调的是 `bind_run` 而不是 `resolve_run`？

> **参考答案**：都会。`resolve_run` 内部最后一步就是 `bind_run`，所以二者都触发校验。CLI 里 supervisor 侧（`cli.main`）先调 `resolve_run(cfg)` 完成解析+校验，再把解析好的 `algorithm` 传给 worker；worker 拿到的是**角色投影后的配置**（`_config_for_role`），注册已在手，只需重新校验这份角色配置，所以调 `bind_run(role_config, resolved.algorithm)`——避免重复解析，又保证了角色配置也过校验。

**练习 2**：如果某个算法支持在线（STREAMING）特征，但它的 provider 里没有对应的 `server_streaming` provider，会在哪里被发现？

> **参考答案**：在注册构造阶段就会被拦下。u4-l2/contracts 里，`make_registration` 强制「契约声明的 (mode, modality) 集合」必须和「provider 实际提供的集合」完全一致（contract/provider parity）。即便漏过了，`_validate_feature_provider` 这里也会再 `server_streaming_for(modality)` 做一次防御性断言。两道关保证不会出现「声明了但没有实现」。

---

### 4.4 build_application_run：衔接到 training.assembly

#### 4.4.1 概念说明

解析和校验都通过后，最后一半步是**装配**：把 `ResolvedRun` 真正变成一组可运行的对象。这件事组合根自己不亲自做（它不懂模型怎么建、优化器怎么配），而是交给专门的下游 `specforge/training/assembly.py`。

`build_application_run` 就是这个「交接点」。它的设计有两个值得注意的点：

1. **幂等入口**：它既能接受 `Config`（内部自动 `resolve_run`），也能直接接受 `ResolvedRun`（跳过解析直接用）。这让「程序化调用」和「CLI 调用」都能用它做统一入口。
2. **交接的是值，不是名字**：它把 `resolved.config` 和 `resolved.algorithm` 作为两个**对象**传给 `build_training_run`，下游**完全不接触** `training.strategy` 这个字符串——解析权被牢牢锁在组合根里。

#### 4.4.2 核心流程

```
build_application_run(run, registry=None)
   │
   ├─ run 是 ResolvedRun？  ──是──▶ resolved = run          （不重复解析）
   │                  └─否──▶ resolved = resolve_run(run, registry)
   │
   ├─ from specforge.training.assembly import build_training_run
   │
   └─ return build_training_run(resolved.config, algorithm=resolved.algorithm)
                                  │
                                  ▼
                       （下游按 deployment.mode / cfg.mode 分发到
                         build_disaggregated_run 或 build_offline_runtime）
```

而 `build_training_run` 的第一步，就是再次确认「传进来的算法和配置是匹配的」——这是组合根和装配层之间的**契约边界**。

#### 4.4.3 源码精读

先看组合根的装配入口：

```python
# specforge/application/composition.py:133-145
def build_application_run(
    run: Config | ResolvedRun,
    registry: AlgorithmRegistry | None = None,
):
    """Build one executable run from the public config contract."""

    resolved = run if isinstance(run, ResolvedRun) else resolve_run(run, registry)
    from specforge.training.assembly import build_training_run

    return build_training_run(
        resolved.config,
        algorithm=resolved.algorithm,
    )
```

[specforge/application/composition.py:133-145](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L133-L145) 是组合根到装配的衔接点。三个要点：

1. `isinstance(run, ResolvedRun)` 决定是否重新解析——CLI 传进来的是 `ResolvedRun`，所以**不会**重复解析（也不会重复校验整个配置）。
2. `build_training_run` 是**懒 import**：只有真正要装配时才 import `training.assembly`，避免组合根模块加载时连带加载重型依赖。
3. 传下去的是 `resolved.config` 和 `resolved.algorithm` 两个对象——**没有 strategy 字符串**。

再看 CLI 是怎么调它的。`cli._train` 按 role 分两路，但都汇到同一个 `build_application_run(resolved).run()`：

```python
# specforge/cli.py:113-146  （_train 函数，节选）
def _train(resolved) -> int:
    from accelerate.utils import set_seed
    cfg = resolved.config
    os.environ["FSDP_SHARDING"] = cfg.training.fsdp_sharding
    set_seed(cfg.training.seed)
    if cfg.training.role == "producer":
        # producer 不初始化 CUDA，直接进组合根
        from specforge.application import build_application_run
        return build_application_run(resolved).run()

    from specforge.distributed import destroy_distributed, init_distributed
    _bootstrap_single_process_env()
    _validate_world_size(cfg, int(os.environ["WORLD_SIZE"]))
    init_distributed(...)
    try:
        import torch.distributed as dist
        _validate_world_size(cfg, dist.get_world_size())
        from specforge.application import build_application_run
        return build_application_run(resolved).run()
    finally:
        destroy_distributed()
```

[specforge/cli.py:113-146](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L113-L146) 是 worker 真正开跑的地方。注意 `_train` 收到的 `resolved` 已经是 `ResolvedRun`（在 `cli.main` 里由 `bind_run` 构造，见下面），所以 `build_application_run` 走的是「不重新解析」分支。producer 分支（[cli.py:121-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L121-L126)）不初始化 CUDA（它只负责捕获/发布特征，见 u7-l5），trainer 分支（[cli.py:142-144](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L142-L144)）先建分布式进程组再装配——两路最终都落到 `build_application_run(resolved).run()`。

那 `resolved` 是怎么在 `cli.main` 里构造的？这是本讲最重要的「调用链」：

```python
# specforge/cli.py:241-267  （main 的 train 分支，节选）
cfg = load_config(args.config, args.overrides)
from specforge.application import bind_run, resolve_run
...
resolved = resolve_run(cfg)                                  # ① 解析+校验（supervisor 侧）
plan = build_launch_plan(resolved.config, algorithm=resolved.algorithm, ...)
if args.plan:
    print(plan.render()); return 0
if plan.kind == "worker":
    os.environ.update(plan.worker_env)
    role_config = _config_for_role(resolved.config, plan.role)   # ② 角色投影
    ...
    _train(bind_run(role_config, resolved.algorithm))            # ③ 重新校验角色配置
return run_commands(plan)
```

[specforge/cli.py:241-267](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L241-L267) 把整条链串了起来：

- ① `resolve_run(cfg)`：supervisor 侧用原始配置做解析+校验，得到 `resolved`（含 `algorithm`）。
- ② `_config_for_role`：把配置投影成当前 worker 角色需要的版本（不改原配置，u3-l1 讲过）。
- ③ `bind_run(role_config, resolved.algorithm)`：**用同一份 algorithm 重新校验角色配置**，再交给 `_train`。

这正好回答了本讲的核心问题——**`resolve_run` 是如何用 `training.strategy` 找到 `AlgorithmRegistration` 的**：在 ① 处，`resolve_run(cfg)` 内部调用 `registry.resolve(cfg.training.strategy)`，按 strategy 名字在 `builtin_algorithm_registry()` 里查到注册；此后整条链路（launch_plan、bind_run、build_application_run、build_training_run）都只传递这个**对象**，谁也不再查名字。

最后看装配层如何「接收」这个对象，并守一道边界：

```python
# specforge/training/assembly.py:549-565
def build_training_run(cfg: Config, *, algorithm: AlgorithmRegistration) -> TrainingRun:
    """Assemble one validated run from an already-resolved algorithm. ..."""

    if algorithm.name != cfg.training.strategy:
        raise ValueError(
            "resolved algorithm does not match training.strategy: "
            f"{algorithm.name!r} != {cfg.training.strategy!r}"
        )
    ...
```

[specforge/training/assembly.py:549-565](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L549-L565) 是装配入口。注意它的 docstring 明说「from an **already-resolved** algorithm」——装配层假定算法已经解析好了，自己**绝不**去查名字。开头那行 `algorithm.name != cfg.training.strategy` 的检查是防御性的：万一有人绕过组合根直接调 `build_training_run` 传错了配对，这里会拦下。后续装配会按 `cfg.deployment.mode` 和 `cfg.mode` 分发到 `build_disaggregated_run` 或 `build_offline_runtime`（[assembly.py:579-633](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L579-L633)），那是 u3-l3 和 u6 的内容。

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：追踪 `cli._train` → `build_application_run(resolved).run()` 的完整链路，说清 `resolve_run` 在其中如何用 `training.strategy` 找到 `AlgorithmRegistration`。

**操作步骤**：

1. 打开 [specforge/cli.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py)，定位 `main` 的 train 分支（约 L241）。
2. 顺着调用往下读，在一张纸上（或注释里）标出下列五个锚点及其所在文件:行号：
   - A. `resolved = resolve_run(cfg)` —— 在哪一行？调用的是哪个模块的哪个函数？
   - B. `bind_run(role_config, resolved.algorithm)` —— 这一步为何不调 `resolve_run`？
   - C. `_train(...)` 进入后，producer / trainer 两路分别在哪一行调 `build_application_run`？
   - D. `build_application_run` 内部，`isinstance(run, ResolvedRun)` 为真时跳过了什么？
   - E. `build_training_run` 开头那行防御性检查，比较的是哪两个值？
3. 针对锚点 A，进一步钻进 [composition.py:40-55](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L40-L55)：确认 `training.strategy` 这个字符串是在哪一行、被传给哪个方法做查表（答案：`registry.resolve(cfg.training.strategy)`）。
4. 再钻进 [registry.py:78-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L78-L84)，确认查表返回的对象同时包含 `spec`（纯契约）和 `providers`（可执行）两个半边。

**需要观察的现象**：

- `training.strategy` 字符串**只在** `resolve_run` → `registry.resolve` 这一处的代码里出现作为「查找键」。
- 从 `bind_run` 之后的整条链路（launch_plan、_train、build_application_run、build_training_run），传递的都是 `resolved.algorithm` 这个**对象**，不再有「按名查表」的动作。

**预期结果**（参考答案）：

| 锚点 | 位置 | 说明 |
|------|------|------|
| A | cli.py 约 L246 `resolve_run(cfg)` | supervisor 侧解析+校验；内部 `registry.resolve(cfg.training.strategy)` |
| B | cli.py 约 L263 `bind_run(role_config, resolved.algorithm)` | 注册已在手，只重新校验角色投影后的配置 |
| C | cli.py L126（producer）/ L144（trainer）`build_application_run(resolved).run()` |
| D | composition.py L139 `isinstance` 为真 → 直接用 `run`，**跳过 `resolve_run`**（不重复解析/校验） |
| E | assembly.py L561 `algorithm.name != cfg.training.strategy` | 防御性校验，确保算法和配置匹配 |

把锚点 A→B→C→D→E 串起来，就是「strategy 字符串 → 查表得到对象 → 对象一路传到底」的完整证据链。

> 说明：本实践是「源码阅读型实践」，不需要 GPU 或真实模型。行号以本讲 HEAD `a4fca14` 为准，若你本地 HEAD 不同，行号可能偏移，但函数名与调用关系不变。

#### 4.4.5 小练习与答案

**练习 1**：`build_application_run` 为什么既能接受 `Config` 又能接受 `ResolvedRun`？这种设计带来了什么好处？

> **参考答案**：用 `isinstance(run, ResolvedRun)` 分流：传 `ResolvedRun` 就直接用（跳过解析），传 `Config` 就内部 `resolve_run`。好处是它成了**统一的公共入口**——程序化调用方可以只给一份 `Config`（简单），CLI 内部可以给已经校验过的 `ResolvedRun`（高效、不重复校验），两种用法共享同一段装配代码。

**练习 2**：`build_training_run` 开头为什么要再检查一次 `algorithm.name != cfg.training.strategy`？既然组合根已经校验过，这不是多余吗？

> **参考答案**：这是「防御性编程」的边界检查。组合根确实校验过，但 `build_training_run` 是装配层的公开函数，理论上可能被其他代码（或未来的重构）直接调用、传入不匹配的配对。这行检查把「算法与配置必须匹配」做成装配层的**入口不变量**，即使有人绕过组合根，也不会静默地用错算法。代价只是一次字符串比较，几乎为零。

---

## 5. 综合实践

**任务**：画出一次 `specforge train` 从「配置文件」到「`build_training_run` 被调用」的完整时序，并标注组合根三件套各自的位置与职责。

**建议步骤**：

1. 选一份真实示例配置（如 `examples/configs/qwen3-8b-eagle3-disaggregated.yaml`），记下它的 `training.strategy` 值。
2. 假设它是一份 online/disaggregated 配置，按下面的骨架补全时序图（用文字或画图工具均可）：

   ```
   用户运行 specforge train --config run.yaml
        │
        ▼
   cli.main: load_config  ──▶ Config（strategy 仍是字符串 "eagle3"）
        │
        ▼
   cli.main: resolve_run(cfg)                 ◀── 组合根·解析
        │     └─ registry.resolve("eagle3")
        │            └─ AlgorithmRegistration(spec, providers)
        │     └─ bind_run(cfg, algorithm)     ◀── 组合根·校验
        │            └─ validate_resolved_run (6 个子校验器)
        │     └─ ResolvedRun(config, algorithm)
        ▼
   build_launch_plan(resolved.config, resolved.algorithm, ...)  ◀── u3-l2
        │
        ▼
   (plan.kind == "worker" 分支)
   role_config = _config_for_role(resolved.config, plan.role)   ◀── 角色投影
        │
        ▼
   _train(bind_run(role_config, resolved.algorithm))            ◀── 重新校验角色配置
        │
        ▼
   _train 内: build_application_run(resolved)                    ◀── 组合根·装配
        │     └─ isinstance(ResolvedRun) 为真 → 不重复解析
        │     └─ build_training_run(resolved.config, algorithm=resolved.algorithm)
        ▼
   assembly.build_training_run                                  ◀── 进入 u6 装配
        └─ algorithm.name == cfg.training.strategy （防御性检查）
        └─ 按 deployment.mode/mode 分发到 disaggregated/offline runtime
   ```

3. 在图上用三种颜色/记号分别标出：
   - **解析**发生的位置（只有一处！）
   - **校验**发生的位置（两处：resolve_run 内、worker 侧 bind_run）
   - **装配**发生的位置（build_application_run → build_training_run）
4. 回答两个问题：
   - `training.strategy` 这个字符串，在整条链路里作为「查找键」用了几次？在哪些位置仍然作为「值」被读取（例如防御性比较）？
   - 如果有人想新增一个算法（u10-l2 的主题），他需要改动本讲的三个文件（composition.py / planning.py / __init__.py）吗？为什么？

**参考答案要点**：

- `training.strategy` 作为**查找键**只出现一次：`resolve_run` → `registry.resolve(cfg.training.strategy)`。作为「值」被读取的地方有：planning.py 第 0 步的名字一致性比较、assembly.py 开头的防御性比较——但这些只是比对，不再查表。
- 新增算法**不需要**改 composition.py / planning.py / __init__.py。因为组合根是「算法无关」的：它只负责「按名查表 + 通用校验」。新算法只要（a）在自己的 `providers.py` 里声明 `AlgorithmSpec` 契约和 providers，（b）在 `builtin.py` 的元组里加一个 `create_registration()`。planning.py 里的六个校验器是**通用**的（读 spec 字段，不针对具体算法），所以自动适用于新算法。

这个综合实践把「组合根只有一处、解析只发生一次、校验集中在装配前」三条核心设计原则串在了一起。

---

## 6. 本讲小结

- **组合根唯一**：`specforge/application/composition.py` 是整个进程里唯一把「配置 + 算法注册」接线的位置，对外只暴露 `resolve_run` / `bind_run` / `build_application_run` 三个动作和 `ResolvedRun` 值对象。
- **`resolve_run` = 解析 + 校验**：它用 `cfg.training.strategy` 在 `builtin_algorithm_registry()` 里 `registry.resolve(name)` 查到 `AlgorithmRegistration`（含 spec 与 providers 两半），再把 `KeyError` 转成 `ValueError`，最后调 `bind_run` 校验。
- **`bind_run` / `validate_resolved_run` = 算法感知校验**：六个子校验器把「特征契约、草稿选项、能力、拓扑、vocab mapping」逐条比对，任何不合法组合在装配前 fail-fast。`bind_run` 单独存在是为了让 worker 侧能用同一份注册重新校验**角色投影后的配置**。
- **`build_application_run` = 幂等装配入口**：既能吃 `Config`（自动解析）也能吃 `ResolvedRun`（跳过解析），把两个对象交给下游 `build_training_run`，**不再传递 strategy 字符串**。
- **解析权被锁死**：下游 `assembly.build_training_run` 假定算法已解析好，开头那行 `algorithm.name != cfg.training.strategy` 只是防御性断言；整条链路里「按名查表」只在 `resolve_run` 发生一次。
- **校验两段式**：supervisor 侧 `resolve_run(cfg)` 校验完整配置，worker 侧 `bind_run(role_config, algorithm)` 校验角色配置——既不重复解析，又保证角色配置也合法。

---

## 7. 下一步学习建议

- **往下看装配内部**：本讲到 `build_training_run` 就停了。接下来读 u6-l1（训练装配 assembly），看 `ModelBundle`、`build_model_bundle` 如何消费 `algorithm.providers.model` 把模型真正建出来。
- **往深看校验依据**：本讲的六个校验器都读 `AlgorithmSpec` 字段。若你对 `feature_contracts` / `capabilities` / `DraftRequirement` 还不熟，回到 u4-l1（算法契约 contracts）巩固。
- **横向看注册体系**：`resolve_run` 依赖的 `AlgorithmRegistry` / `AlgorithmRegistration` 在 u4-l2（算法注册表 registry 与 builtin）有完整讲解；草稿架构这条**独立轴线**在 u4-l4（草稿模型注册表）。
- **动手扩展**：学完本讲后，直接跳到 u10-l2（新增一个训练算法），你会发现在 `builtin.py` 加一行注册后，本讲的组合根无需任何改动就能解析并校验你的新算法——这是「组合根算法无关」设计的直接回报。
