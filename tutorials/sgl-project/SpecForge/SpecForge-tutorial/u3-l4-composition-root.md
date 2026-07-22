# 应用组合根 composition

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出「组合根（composition root）」是什么，以及为什么 SpecForge 全项目只允许有**一个**组合根。
- 解释 `ResolvedRun` 这个不可变值对象的含义：它把一份已校验配置和它对应的**那一个**算法注册绑在一起。
- 画出 `resolve_run → bind_run → validate_resolved_run → build_application_run` 的调用顺序，并指出每一步在哪一个文件里。
- 说明 `training.strategy` 这个字符串是如何在 `resolve_run` 内被翻译成一个 `AlgorithmRegistration`（含纯值契约 `spec` 与可执行端口 `providers` 两半）。
- 追踪从 `cli._train` 到 `build_application_run(resolved).run()` 的完整链路，理解组合根如何衔接 `training.assembly`。

## 2. 前置知识

本讲是「入口与启动链路」单元的收口，承接 u3-l3（拓扑构建器 launch）与 u4-l1（算法契约 contracts）。阅读前，请先在脑中确认以下几点（这些都在前置讲义中建立过）：

- **类型化配置**：SpecForge 用 Pydantic 描述 YAML，七段配置（model/data/training/tracking/profiling/runtime/deployment）加载后得到一个 `Config` 对象，未知字段直接报错（见 u2-l2）。
- **算法注册表**：`training.strategy` 在 schema 层只是一个普通字符串（默认 `"eagle3"`），真正把它变成可执行行为的是注册表按名查表（见 u2-l2、u4-l1）。
- **组合根**：`application/composition.py` 是全项目唯一把「配置 + 算法注册 + 训练器」拼装到一起的地方（见 u1-l5）。
- **两条正交轴线**：数据模式 online/offline（由 `data.hidden_states_path` 是否为空决定，也是 `Config.mode` 推导属性）与部署模式 colocated/disaggregated（由 `deployment.mode` 决定）。本讲的校验逻辑大量围绕这两条轴线展开。

本讲还会用到两个软件工程术语，先解释清楚：

- **组合根（composition root）**：依赖注入（DI）里的概念，指应用中**唯一**一处「把抽象绑定到具体实现」的位置。SpecForge 没有用 DI 容器框架，而是用 `composition.py` 这个普通模块充当组合根：所有「按名查算法、把算法塞进训练器」的决定都集中在这里，其他模块只消费结果、不做绑定。
- **不可变值对象（frozen dataclass）**：用 `@dataclass(frozen=True)` 声明，构造后字段不可改。SpecForge 的 `ResolvedRun`、`AlgorithmRegistration`、`AlgorithmSpec` 都是不可变的，保证「解析一次、到处只读」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [specforge/application/composition.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py) | **唯一的组合根**。定义 `ResolvedRun` 值对象，以及 `resolve_run`/`bind_run`/`build_application_run`/`resolve_offline_capture` 四个公开函数。 |
| [specforge/application/planning.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py) | **算法感知的校验层**。`validate_resolved_run` 在这里，由六道针对性校验组成。 |
| [specforge/application/\_\_init\_\_.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/__init__.py) | 包入口，把组合根的公开 API 重新导出，外部统一从 `specforge.application` 导入。 |
| [specforge/algorithms/registry.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py) | `AlgorithmRegistration`（spec+providers 两半）与 `AlgorithmRegistry`（不可变目录、按名 `resolve`）。 |
| [specforge/algorithms/builtin.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py) | `builtin_algorithm_registry()` 组装五个内置算法，返回全新不可变目录。 |
| [specforge/cli.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py) | 调用方。`main()` 的 train 分支先 `resolve_run`，worker 分支再 `_train(bind_run(...))`。 |
| [specforge/training/assembly.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py) | 组合根的下游。`build_training_run` 接收已解析的 `algorithm`，装配出可执行的 `TrainingRun`。 |

一句话定位：`composition.py` 是「决定用哪个算法」的唯一场所，`planning.py` 是「校验这个决定是否合法」的场所，`assembly.py` 是「按这个决定把对象造出来」的场所。

## 4. 核心概念与源码讲解

### 4.1 组合根与 ResolvedRun：把配置与算法绑成一个不可变值

#### 4.1.1 概念说明

先建立一个直觉：SpecForge 支持 5 种草稿算法（eagle3/peagle/dflash/domino/dspark），它们共用同一个 `specforge train` 入口，仅靠 `training.strategy` 这一个字符串区分。这意味着系统在某个时刻必须做一次「翻译」——把字符串 `"eagle3"` 变成「EAGLE3 算法的全部行为描述」。

这次翻译**只允许发生在一个地方**，就是组合根。为什么要集中？

- 如果到处都能查表，就会出现「A 模块查到了 eagle3、B 模块又查了一次」的不一致风险。
- 校验也必须集中在翻译的瞬间：算法声明自己只支持 offline + text，那一份 online 的配置就该在翻译时立刻被拒绝，而不是拖到训练中途崩溃。

`ResolvedRun` 就是这次翻译的**产物**——一个不可变的、配好算法的「resolved 配置」。它的定义极其简单，只有两个字段：

```python
@dataclass(frozen=True)
class ResolvedRun:
    """A validated config paired with its one algorithm registration."""

    config: Config
    algorithm: AlgorithmRegistration
```

参见 [specforge/application/composition.py:12-17](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L12-L17)，这段代码把「一份已校验配置」和「它对应的那一个算法注册」捆成一个 frozen 值对象。一旦构造完成，下游所有 builder 都只读取它，不再查名、不再做绑定决定。

这里的 `algorithm` 字段类型是 `AlgorithmRegistration`，它本身也是不可变的，并且**内含两半**：一半是纯值契约 `spec`（描述「我需要什么、支持什么」），一半是可执行端口 `providers`（描述「具体怎么做」）。

```python
@dataclass(frozen=True)
class AlgorithmRegistration:
    """One lookup result containing pure metadata and executable providers.

    Keeping the two halves in one registration avoids parallel spec/provider
    registries.  Planning reads ``spec``; the composition root alone reads
    ``providers``.
    """

    spec: AlgorithmSpec
    providers: object
```

参见 [specforge/algorithms/registry.py:11-18](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L11-L18)。注意 docstring 里的一句话：**「Planning reads `spec`; the composition root alone reads `providers`.」**（校验层只读纯值契约 `spec`，只有组合根才读可执行端口 `providers`）。这是 SpecForge 刻意划的边界：

- `spec`（`AlgorithmSpec`）是纯数据，不含任何模型类、工厂函数，可以被序列化、可以在不导入 torch 的情况下读取。校验层 `planning.py` 只用它。
- `providers` 是可执行对象（含 `build_draft`、`build_collator` 等工厂），只有真正要造模型的组合根/装配层才会碰它。

这条边界让「检查一份配置是否合法」与「真正训练」彻底解耦：`specforge train --plan` 可以在不导入 torch、不占 GPU 的前提下完成全部校验（详见 u2-l3）。

#### 4.1.2 核心流程

从「一份 YAML」到「一个 `ResolvedRun`」，组合根的职责可以画成下面这条单向流水线：

```
Config（已加载、已应用 overrides）
        │
        │  resolve_run(cfg)
        ▼
registry.resolve(cfg.training.strategy)   ← 按名查表，唯一一次「翻译」
        │
        │  bind_run(cfg, algorithm)
        ▼
validate_resolved_run(cfg, algorithm)      ← 算法感知校验（见 4.3）
        │
        ▼
ResolvedRun(config=cfg, algorithm)         ← 不可变产物
```

要点：

1. **翻译只发生一次**：`training.strategy` 字符串只在 `resolve_run` 里被查表一次，之后全程传递 `AlgorithmRegistration` 对象本身。
2. **校验紧跟翻译**：查到算法后立刻校验配置与算法是否兼容，失败就 `raise ValueError`，绝不把非法状态带进下游。
3. **产物不可变**：`ResolvedRun` 是 frozen 的，下游无法篡改「这次 run 用哪个算法」。

#### 4.1.3 源码精读

先看组合根的公开导出，理解它的「API 表面」有多大。[specforge/application/\_\_init\_\_.py:3-10](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/__init__.py#L3-L10) 只重新导出 6 个名字：`ResolvedRun`、`ResolvedOfflineCapture`、`bind_run`、`build_application_run`、`resolve_offline_capture`、`resolve_run`。外部代码统一写 `from specforge.application import resolve_run`，不直接深入 `composition` 子模块——这是组合根「唯一入口」原则的体现。

再看 `AlgorithmRegistration` 的构造期自检。[specforge/algorithms/registry.py:23-33](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L23-L33) 在 `__post_init__` 里强制三件事：`spec` 必须是 `AlgorithmSpec`、`providers` 不能为 `None`、且 `providers.algorithm_name` 必须等于 `spec.name`。这保证「两半」永远指向同一个算法名，不会出现 spec 说是 eagle3、providers 却是 dflash 的错配。

最后，`AlgorithmSpec` 自身用 `_assert_pure_value` 递归守护「纯值」不变量——参见 [specforge/algorithms/contracts.py:42-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L42-L68)：任何 `type` 或 `callable` 都会被拒绝。这段代码是「spec 不含可执行对象」这条边界的强制器，也是 u4-l1 的核心，本讲只需记住它的结论：**`spec` 里连一个函数指针都塞不进去**，所以校验层读它绝对安全。

#### 4.1.4 代码实践

这是一个源码阅读型实践，目标是建立对「两半」结构的肌肉记忆。

1. **实践目标**：在不导入 torch 的前提下，说出 `AlgorithmRegistration` 的 `spec` 和 `providers` 各自能装什么、不能装什么。
2. **操作步骤**：
   - 打开 [specforge/algorithms/registry.py:11-37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L11-L37)，确认 `AlgorithmRegistration` 只有两个字段。
   - 打开 [specforge/algorithms/contracts.py:265-309](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L265-L309)，找到 `AlgorithmSpec.__post_init__` 末尾的 `_assert_pure_value(self, path="AlgorithmSpec")` 调用。
   - 对比 [specforge/algorithms/common/providers.py:581-637](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L581-L637) 的 `AlgorithmProviders`，它里面装的是 `StepProvider`/`ModelProvider` 等**含工厂函数**的对象。
3. **需要观察的现象**：`spec` 一侧的每个字段都是 `str`/`int`/`frozenset`/`tuple` 这类纯值；`providers` 一侧则到处是 `Factory = Callable[..., Any]`。
4. **预期结果**：你能用一句话回答——「为什么 `planning.py` 只读 `spec` 而不碰 `providers`？」答：因为 `spec` 被 `_assert_pure_value` 保证为纯值，读取它不需要任何重依赖；而 `providers` 含可执行对象，只有真正装配时才该触碰。
5. **运行结果**：待本地验证（本实践为纯源码阅读，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：假如有人想在 `AlgorithmSpec` 里塞一个 `model_cls: type` 字段方便后续直接构造模型，会发生什么？

**参考答案**：会在 `AlgorithmSpec.__post_init__` 调用 `_assert_pure_value` 时抛 `TypeError`，因为 `type` 是可执行对象。SpecForge 刻意把模型类挡在纯值契约之外，模型构造只能经 `providers.model.build_draft` 这类工厂端口进入，由组合根在装配阶段才触碰。

**练习 2**：`ResolvedRun` 为什么用 `frozen=True`？

**参考答案**：为了让「这次 run 用哪个算法」在解析之后不可篡改。下游所有 builder 都拿到同一个不可变引用，杜绝了「某个 builder 偷偷换算法」的可能，也让多进程分发（role 投影）只能通过重新 `bind_run` 造一个新值，而不是就地改字段。

---

### 4.2 resolve_run 与 bind_run：按 training.strategy 查表并校验绑定

#### 4.2.1 概念说明

`ResolvedRun` 是产物，而**造出产物**的两个函数是 `resolve_run` 和 `bind_run`。它们分工很清楚：

- `resolve_run(cfg, registry=None)`：负责「查表」。读 `cfg.training.strategy`，在注册表里找到对应的 `AlgorithmRegistration`，然后把配置和算法交给 `bind_run`。如果调用方没传 `registry`，就用内置目录 `builtin_algorithm_registry()`。
- `bind_run(cfg, algorithm)`：负责「绑定 + 校验」。它假定算法**已经被查到了**（外部直接把 `AlgorithmRegistration` 传进来），只做一件事——调用 `validate_resolved_run(cfg, algorithm)` 校验二者兼容，通过后返回 `ResolvedRun`。

为什么拆成两个？因为存在「算法已经查到、只需重新校验」的场景。最典型的就是 **role 投影**：cli 在启动 worker 前，会把同一份配置投影成 producer 或 consumer 两个角色（详见 u3-l1 的 `_config_for_role`）。此时算法没变（还是同一个 `resolved.algorithm`），但配置变了（producer 会关掉 profiling、清空 managed_local 等），所以**必须重新校验**投影后的配置是否仍与算法兼容——这正是 `bind_run(role_config, resolved.algorithm)` 的用途，它跳过查表、只重做校验。

#### 4.2.2 核心流程

`resolve_run` 的内部流程（[specforge/application/composition.py:40-55](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L40-L55)）：

```
resolve_run(cfg, registry=None)
  ├── registry is None?  →  builtin_algorithm_registry()   # 默认用内置目录
  ├── algorithm = registry.resolve(cfg.training.strategy)  # 按名查表
  │        └─ 查不到 → KeyError → 转成 ValueError
  └── return bind_run(cfg, algorithm)
                 └── validate_resolved_run(cfg, algorithm)  # 进入 4.3
                 └── return ResolvedRun(config=cfg, algorithm=algorithm)
```

查表本身在 `AlgorithmRegistry.resolve` 里——参见 [specforge/algorithms/registry.py:78-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L78-L84)：线性扫描 `_registrations`，按 `name` 匹配，找不到就抛 `KeyError` 并列出所有已注册算法名。注意 `resolve_run` 把这个 `KeyError` **转成了 `ValueError`**（[composition.py:50-53](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L50-L53)），因为对调用方（cli）而言，「策略名拼错」属于配置错误，归一成 `ValueError` 更统一。

内置目录的组装在 [specforge/algorithms/builtin.py:13-16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py#L13-L16)：每次调用都返回一个**全新的** `AlgorithmRegistry`（五个算法：eagle3/peagle/dflash/domino/dspark），不做模块级缓存。这点很关键——它意味着「注册表是不可变值」，没有全局可变状态，测试和并发都更安全。

#### 4.2.3 源码精读

`resolve_run` 全文（[specforge/application/composition.py:40-55](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L40-L55)）：

```python
def resolve_run(cfg, registry=None):
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

注意三处设计：

1. **延迟导入**：`builtin_algorithm_registry` 在函数内部 `import`，而不是写在模块顶部。这避免「导入 `composition` 就连带导入所有算法 providers（进而导入 torch/transformers）」。组合根本身保持轻量，重依赖按需加载。
2. **`exactly once` 语义**：docstring 写明「把所有算法相关行为解析且仅解析一次」。查表、校验都在这一步完成，之后产物 `ResolvedRun` 被无重复地传递。
3. **错误归一**：`KeyError → ValueError`，让「未知策略名」和「配置与算法不兼容」走同一种异常类型。

`bind_run` 全文（[specforge/application/composition.py:31-37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L31-L37)）：

```python
def bind_run(cfg, algorithm):
    """Validate a role-projected config against an existing registration."""

    from specforge.application.planning import validate_resolved_run
    validate_resolved_run(cfg, algorithm)
    return ResolvedRun(config=cfg, algorithm=algorithm)
```

它的 docstring 一语道破用途：**「Validate a role-projected config against an existing registration.」**（针对一个已存在的注册，校验角色投影后的配置）。它不查表，只校验。注意 `validate_resolved_run` 也是延迟导入，同样是为了保持组合根模块的轻量。

查表那一步 `AlgorithmRegistry.resolve`（[specforge/algorithms/registry.py:78-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L78-L84)）的实现非常朴素——线性查找。这是因为算法总数只有个位数，没必要上字典；同时注册表构造时已经做过去重校验（[registry.py:60-63](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L60-L63)），保证名字唯一。

#### 4.2.4 代码实践

1. **实践目标**：确认「strategy 名 → AlgorithmRegistration」的翻译确实只发生在 `resolve_run` 这一处。
2. **操作步骤**：
   - 用 `Grep` 在整个 `specforge/` 下搜索 `registry.resolve` 与 `builtin_algorithm_registry` 的调用点。
   - 你应该只看到两处：`composition.py` 的 `resolve_run`（查表），以及 `resolve_offline_capture` 里复用 `resolve_run`（不重复查表）。
3. **需要观察的现象**：除了组合根，`assembly.py`、`trainer.py`、`strategies/` 都**不查表**，它们只接收一个已经造好的 `algorithm: AlgorithmRegistration` 参数。
4. **预期结果**：你能得出结论——如果把 `training.strategy=eagle3` 改成 `training.strategy=eagle4`（一个不存在的名字），错误会在 `resolve_run` 处以 `ValueError` 抛出，错误信息里会列出全部已注册算法名。
5. **运行结果**：待本地验证（可写一个最小脚本 `python -c "from specforge.application import resolve_run; from specforge.config import Config; ..."` 触发，但构造合法 Config 较繁琐，建议直接读 cli 报错路径）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `bind_run` 要和 `resolve_run` 拆开，而不是合并成一个 `resolve_and_bind(cfg)`？

**参考答案**：因为 role 投影后，算法不变、配置变，需要「跳过查表、只重做校验」。`bind_run(role_config, resolved.algorithm)` 正好满足这个需求——它接收外部传入的 `algorithm`，只跑 `validate_resolved_run`。若两者合并，role 投影时就得重新查一次表，既多余又可能引入不一致。

**练习 2**：`resolve_run` 把 `KeyError` 转成 `ValueError`，为什么不直接让 `KeyError` 传出去？

**参考答案**：统一错误口径。cli 的 `load_config`→`resolve_run` 链路里，所有「配置有问题」都应归一成 `ValueError`，方便上层用同一套 `except` 处理并向用户友好报错；`KeyError` 在语义上更像「字典缺键」的内部异常。

---

### 4.3 validate_resolved_run：算法感知的六道校验

#### 4.3.1 概念说明

`validate_resolved_run` 是组合根的「质检车间」。它做的事很专一：拿一份配置和一个算法注册，检查二者是否兼容，不兼容就 `raise ValueError`。它位于 [specforge/application/planning.py:189-205](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L189-L205)。

它的核心特点是**算法感知（algorithm-aware）**：普通 schema 校验（Pydantic 的 `extra=forbid`、字段类型）只能查「这份 YAML 语法对不对」，而 `validate_resolved_run` 能查「这份配置配 EAGLE3 这个具体算法，对不对」。比如：

- schema 不知道 EAGLE3 支持哪些 attention backend；
- `validate_resolved_run` 知道，因为算法的 `AlgorithmCapabilities` 声明了 `attention_backends`。

「方法 × 拓扑」的不支持组合，正是**在这里**被 fail-fast 拦截的（承接 u1-l1 提到的「不支持的组合在校验或装配阶段直接报错」）。

注意它只读 `algorithm.spec`（纯值契约），不碰 `algorithm.providers`（可执行端口）——除了 `_validate_feature_provider` 里一处防御性 `resolve`，目的是把 modality 失败留在这个通用边界（见源码注释 [planning.py:31-37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L31-L37)）。

#### 4.3.2 核心流程

`validate_resolved_run` 先做一个一致性断言（确保传入的 algorithm 名与 `cfg.training.strategy` 一致），然后顺序跑六道校验：

```
validate_resolved_run(cfg, algorithm)
  ├── assert algorithm.name == cfg.training.strategy   # 防御性断言
  ├── mode = _feature_mode(cfg)                         # offline / streaming
  ├── _validate_feature_provider     # (mode, modality) 是否被算法声明
  ├── _validate_draft_options        # draft 配置/覆盖是否被算法允许
  ├── _validate_algorithm_capabilities# attention_backend / batch_size / 捕获层 / compact_teacher
  ├── _validate_training_topology    # online↔disaggregated、tp/SP 等拓扑约束
  └── _validate_vocab_mapping        # disaggregated 是否需要 vocab_mapping_path
```

其中 `_feature_mode`（[planning.py:10-11](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L10-L11)）把 `Config.mode`（`"offline"`/`"online"`）映射成算法契约里的 `FeatureMode.OFFLINE`/`FeatureMode.STREAMING`——这是配置世界与算法契约世界之间的「翻译器」。

六道校验大致可以分成三类：

| 类别 | 校验函数 | 检查的问题举例 |
| --- | --- | --- |
| 算法能力 | `_validate_feature_provider`、`_validate_algorithm_capabilities`、`_validate_draft_options` | 算法是否声明了这个 (mode, modality)？attention_backend 是否被支持？draft 层数覆盖是否被允许？ |
| 拓扑约束 | `_validate_training_topology` | online 是否 disaggregated？离线是否 tp_size=1？USP 是否仅限 offline？ |
| 数据契约 | `_validate_vocab_mapping` | disaggregated 模式是否要求 vocab_mapping_path？ |

#### 4.3.3 源码精读

先看入口 [specforge/application/planning.py:189-205](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L189-L205)：

```python
def validate_resolved_run(cfg, algorithm):
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

最顶上的 `algorithm.name != cfg.training.strategy` 是一道防御性断言：正常流程下二者必然相等（因为 algorithm 就是按 strategy 查出来的），但 `bind_run` 允许外部直接传 algorithm，所以这里兜底防止「传错算法」。

挑两道最能体现「算法感知」的校验细看。

**第一道：特征提供者校验** [planning.py:14-37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L14-L37)。它问算法的 `spec.supports(mode, modality)`：你这个算法到底声明了「offline + text」这个组合吗？没声明就直接报错，并列出算法支持的全部 `(mode, modality)` 组合供用户参考。这把「方法 × 模式」的不支持组合挡在了训练之前。

**第二道：训练拓扑校验** [planning.py:124-169](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L124-L169)。这一道信息量最大，它编码了 SpecForge 的拓扑铁律，举几条关键的：

- offline 且 `tp_size != 1` → 报错（[planning.py:129-134](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L129-L134)）：离线特征消费者不做 trainer 张量并行，每个非 SP rank 各拿一份数据分片。
- streaming（online）但 `deployment.mode != "disaggregated"` → 报错（[planning.py:135-140](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L135-L140)）：在线训练恒为 disaggregated，colocated online 已不再支持（承接 u3-l3）。
- online 但 `target_backend != "sglang"` → 报错（[planning.py:141-145](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L141-L145)）：在线特征必须由外部 SGLang 服务捕获。
- online 但 disaggregated backend 不是 mooncake → 报错（[planning.py:146-151](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L146-L151)）：在线 disaggregated 必须用 mooncake 传输。
- online 且 `tp/sp_ulysses/sp_ring` 任一 >1 → 报错（[planning.py:157-166](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L157-L166)）：在线 consumer 把每个 trainer rank 都用于数据并行，TP 要配在外部服务上。
- `attention_backend == "usp"` 但不是 offline → 报错（[planning.py:168-169](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L168-L169)）：USP 目前只支持离线特征。

这一道校验几乎就是 u3-l3「四条路径」表的政策编码——读这段代码等于在读 SpecForge 的部署规则手册。

#### 4.3.4 代码实践

1. **实践目标**：通过故意构造非法组合，理解每道校验拦截的是哪一类错误。
2. **操作步骤**：
   - 选一个 online 示例配置（如 `examples/configs/qwen3-8b-dflash-online.yaml`，若不存在则任选一个 online 配置）。
   - 用 `--plan` 叠加一个非法覆盖，例如把 `deployment.mode` 强行改成 `local_colocated`（或把 `model.target_backend` 改成非 sglang）：
     ```
     specforge train --config <online.yaml> --plan deployment.mode=local_colocated
     ```
   - 观察报错信息。
3. **需要观察的现象**：命令应**不启动训练、不占 GPU** 就直接报 `ValueError`，且报错文案能精确指向 `_validate_training_topology` 里的某一条（例如 "online training requires deployment.mode=disaggregated"）。
4. **预期结果**：你亲自验证了「online 必须 disaggregated」这条铁律是在 `validate_resolved_run` 阶段、而非训练中途被拦截的。这正是 `--plan` 能作为「零开销体检工具」的根本原因。
5. **运行结果**：待本地验证（需要先按 u1-l2 装好环境；若仅做源码阅读，可直接在 [planning.py:135-140](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L135-L140) 对照错误文案）。

#### 4.3.5 小练习与答案

**练习 1**：一份 offline 配置误设了 `training.tp_size=2`，会在哪道校验失败？为什么离线不允许 trainer 张量并行？

**参考答案**：在 `_validate_training_topology` 失败（[planning.py:129-134](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L129-L134)）。离线特征消费者不做 trainer TP，而是让每个非 SP rank 各拿一份数据分片（数据并行），因此要求 `tp_size=1`。

**练习 2**：`validate_resolved_run` 顶部为什么要断言 `algorithm.name == cfg.training.strategy`？既然 algorithm 是按 strategy 查出来的，二者必然相等。

**参考答案**：正常 `resolve_run` 流程下确实相等，但 `bind_run` 允许外部**直接传入** `algorithm`（如 role 投影后复用同一个 `resolved.algorithm`）。这道断言是给「外部直接调用 bind_run」的兜底，防止把 A 算法的 registration 错配到 strategy=B 的配置上。

**练习 3**：online 配置如果同时满足「disaggregated + target_backend=sglang + mooncake」，但 `training.sp_ulysses_size=2`，能通过校验吗？

**参考答案**：不能。会被 [planning.py:157-166](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L157-L166) 拦截：在线 consumer 把每个 trainer rank 都用于数据并行，要求 `tp_size/sp_ulysses_size/sp_ring_size` 全为 1，TP 要配在外部 SGLang 服务上。

---

### 4.4 build_application_run：装配可执行 run 并衔接 training.assembly

#### 4.4.1 概念说明

前面三步（resolve/bind/validate）解决的是「这次 run 用哪个算法、配置是否合法」，但还没有**任何一个真实对象被造出来**——没有草稿模型、没有 tokenizer、没有优化器。把这些重量级对象装配出来的，是 `build_application_run`。

它是组合根对外的**总装配入口**，签名很宽容（[specforge/application/composition.py:133-145](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L133-L145)）：既接收 `ResolvedRun`，也接收裸 `Config`。如果是裸 `Config`，它会先 `resolve_run` 把算法解析出来；如果已经是 `ResolvedRun`，就直接用，避免重复解析。装配本身委托给 `training.assembly.build_training_run`，组合根只负责「确保 algorithm 已解析、然后把球传给装配层」。

它返回的对象有一个 `.run()` 方法——这是一个带完整生命周期（成功/失败/收尾钩子）的可执行 run。调用方（cli 的 `_train`）拿到后直接 `build_application_run(resolved).run()` 就启动了训练。

#### 4.4.2 核心流程

`build_application_run` 的流程（[composition.py:133-145](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L133-L145)）：

```
build_application_run(run, registry=None)
  ├── run 是 ResolvedRun?  → 直接用
  │   run 是 Config?       → resolve_run(run, registry)   # 兜底解析
  ├── 延迟导入 build_training_run
  └── return build_training_run(resolved.config, algorithm=resolved.algorithm)
              │
              ▼
        TrainingRun（含 trainer / execute / 生命周期钩子）
```

关键设计：

1. **双入口归一**：`Config | ResolvedRun` 两种入参都支持，但内部统一收敛到「先有 ResolvedRun，再装配」。这样既方便外部「给个 Config 就能跑」的简化用法，又允许 cli 这种已经 resolve 过的场景「跳过重复解析」。
2. **装配外移**：组合根**不自己造模型**，而是把 `resolved.config` 和 `resolved.algorithm` 一起交给 `build_training_run`。组合根只管「解析与校验」，装配细节全部在 `training/assembly.py`（这是 u6-l1 的主题）。
3. **延迟导入**：`from specforge.training.assembly import build_training_run` 写在函数体内，避免导入组合根就连带导入 torch/transformers。

#### 4.4.3 源码精读

`build_application_run` 全文（[specforge/application/composition.py:133-145](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L133-L145)）：

```python
def build_application_run(run, registry=None):
    """Build one executable run from the public config contract."""

    resolved = run if isinstance(run, ResolvedRun) else resolve_run(run, registry)
    from specforge.training.assembly import build_training_run

    return build_training_run(
        resolved.config,
        algorithm=resolved.algorithm,
    )
```

第一行 `resolved = run if isinstance(run, ResolvedRun) else resolve_run(run, registry)` 是「双入口归一」的核心：用 `isinstance` 判断，已是 `ResolvedRun` 就直接用，否则当 `Config` 解析。

下游 `build_training_run` 的入口（[specforge/training/assembly.py:549-565](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L549-L565)）：

```python
def build_training_run(cfg, *, algorithm: AlgorithmRegistration) -> TrainingRun:
    """Assemble one validated run from an already-resolved algorithm. ..."""

    if algorithm.name != cfg.training.strategy:
        raise ValueError(
            "resolved algorithm does not match training.strategy: "
            f"{algorithm.name!r} != {cfg.training.strategy!r}"
        )
    ...
```

注意两件事：

1. `algorithm` 是**关键字必传参数**（`*, algorithm`），强制调用方必须显式传一个已解析的算法——装配层**绝不自己查表**，完全依赖组合根喂进来的 `algorithm`。这呼应了 u3-l3 的「统一训练路径汇聚」：所有拓扑构建器（offline/disagg offline/online consumer）最终都汇聚到这个 `build_training_run`，差异只体现在 `cfg` 里，而不在「查哪个算法」上。
2. 装配层入口**又做了一次** `algorithm.name != cfg.training.strategy` 断言（[assembly.py:561-565](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L561-L565)）。这是 `validate_resolved_run` 顶部那道断言的「双层防御」——组合根校验一次，装配层入口再兜底一次，确保即便有人绕过组合根直接调 `build_training_run`，也能及时发现算法与 strategy 不匹配。

装配产物 `TrainingRun` 有一个 `run()` 方法（[specforge/training/assembly.py:78-80](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L78-L80)），这就是 cli 里 `build_application_run(resolved).run()` 那个 `.run()` 的真身。

#### 4.4.4 代码实践（本讲核心实践）

这是本讲要求的链路追踪实践：**追踪从 `cli._train` 调用 `build_application_run(resolved).run()` 的链路，说明 `resolve_run` 在其中如何用 `training.strategy` 找到对应的 `AlgorithmRegistration`。**

1. **实践目标**：把「字符串 strategy → 可执行训练」这条主轴在源码里完整走一遍，标出三个关键点。
2. **操作步骤**：
   - **第 1 步：strategy 被翻译成 AlgorithmRegistration**。打开 [specforge/cli.py:241-267](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L241-L267)，找到 `main()` 的 train 分支。`cfg = load_config(...)` 之后紧接着 `resolved = resolve_run(cfg)`——**这是 strategy 字符串被翻译的唯一瞬间**。跟踪进 [composition.py:40-55](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L40-L55)：`registry.resolve(cfg.training.strategy)` 按 strategy 名在内置目录里查到 `AlgorithmRegistration`（含 `spec` + `providers` 两半），再经 `bind_run` 校验。
   - **第 2 步：role 投影后重新绑定**。仍在 [cli.py:258-263](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L258-L263)：worker 分支里 `_config_for_role(resolved.config, plan.role)` 把配置投影成具体角色（producer/consumer/all），然后 `bind_run(role_config, resolved.algorithm)` **复用同一个 algorithm、只重做校验**，造出角色专用的 `ResolvedRun`。注意这里**不重新查表**——algorithm 直接复用第 1 步的结果。
   - **第 3 步：装配并启动**。打开 [specforge/cli.py:113-146](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L113-L146) 的 `_train`：producer 角色不初始化 CUDA，直接 `build_application_run(resolved).run()`；trainer 角色（all/consumer）先 `init_distributed` 再 `build_application_run(resolved).run()`。两条路最终都汇入 `build_application_run` → [composition.py:142-145](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L142-L145) 的 `build_training_run(resolved.config, algorithm=resolved.algorithm)`，返回带 `.run()` 的 `TrainingRun`。
3. **需要观察的现象**：整条链路里，`training.strategy` 这个字符串**只在第 1 步被读了一次**（`registry.resolve`）。之后全程传递的是 `AlgorithmRegistration` 对象本身——`build_launch_plan` 收 `algorithm=`、`bind_run` 收 `algorithm`、`build_training_run` 收 `algorithm=`，没有任何下游再读 strategy 字符串去查表。
4. **预期结果**：你能画出下面这条调用序列（以 trainer 角色为例）：
   ```
   cli.main()
     └─ cfg = load_config(...)                      # Config，含 strategy 字符串
     └─ resolved = resolve_run(cfg)                 # ★ strategy → AlgorithmRegistration
     └─ plan = build_launch_plan(resolved.config, algorithm=resolved.algorithm, ...)
     └─ (plan.kind == "worker")
          └─ role_config = _config_for_role(resolved.config, plan.role)
          └─ _train( bind_run(role_config, resolved.algorithm) )   # 复用 algorithm，重校验
               └─ init_distributed(...)             # trainer 角色
               └─ build_application_run(resolved).run()            # 已是 ResolvedRun，不再 resolve
                    └─ build_training_run(resolved.config, algorithm=resolved.algorithm)
                         └─ TrainingRun.run()
   ```
   并能回答核心问题：`resolve_run` 用 `cfg.training.strategy` 在 `builtin_algorithm_registry()` 里 `registry.resolve(name)` 查到一个 `AlgorithmRegistration`——它的 `spec` 是纯值契约（EAGLE3 支持哪些 mode/modality/attention backend），`providers` 是可执行端口（如何造草稿模型、如何造 collator）；之后这个对象被原封不动地传递到装配层，装配层据此造出真实的训练对象。
5. **运行结果**：待本地验证（本实践为源码阅读型，无需启动训练；若想看真实报错路径，可对任意配置跑 `specforge train --plan` 并故意把 `training.strategy` 改成一个不存在的名字，观察 `resolve_run` 抛出的 `ValueError` 列出全部已注册算法名）。

#### 4.4.5 小练习与答案

**练习 1**：`build_application_run` 为什么接受 `Config | ResolvedRun` 两种类型？

**参考答案**：为了兼顾两种调用方。简化用法下，外部可以直接传一份 `Config`，函数内部 `resolve_run` 兜底解析；而 cli 这种已经走过 `resolve_run` + role 投影的场景，会传一个现成的 `ResolvedRun`，此时 `isinstance` 判定后直接复用，**跳过重复解析与重复校验**。

**练习 2**：`build_training_run` 为什么在入口又做一次 `algorithm.name != cfg.training.strategy` 断言？组合根不是已经校验过了吗？

**参考答案**：双层防御。组合根的 `validate_resolved_run` 校验过一次，但装配层不能假设所有调用方都走了组合根——可能有人绕过组合根直接调 `build_training_run`。入口断言保证即便绕过组合根，也能及时发现「传进来的 algorithm 与 cfg.training.strategy 不匹配」，避免用 A 算法的 providers 去装配 strategy=B 的配置。

**练习 3**：producer 角色和 trainer 角色在 `_train` 里都调用 `build_application_run(resolved).run()`，二者有何不同？

**参考答案**：producer（server-capture/offline-ingest）**不初始化 CUDA、不建进程组**，因为它只负责发布特征引用、不拥有 trainer 进程组（见 [cli.py:121-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L121-L126) 的注释）；trainer（all/consumer）先 `_bootstrap_single_process_env` + `init_distributed` 建好进程组，再用 `try/finally` 保证 `destroy_distributed`，最后才调 `build_application_run(resolved).run()`。两者都汇入同一个组合根入口，差异只在「是否先初始化分布式」。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「全链路追踪 + 故障注入」任务：

**任务**：选定一个 checked-in 示例配置（推荐 `examples/configs/qwen3-8b-eagle3-disaggregated.yaml`），完成以下三小问，并把结论填进一张表。

1. **追踪 strategy 的命运**：从 `specforge train --config <该yaml> --plan` 开始，在源码里标出 `training.strategy`（其值应为 `eagle3`）这个字符串被读取的全部位置。确认它只在 `resolve_run`→`registry.resolve` 处被「翻译」一次，之后全程以 `AlgorithmRegistration` 对象传递。
2. **读懂校验报告**：对同一配置跑两次 `--plan`，分别叠加一个合法覆盖和一个非法覆盖：
   - 合法：`training.max_steps=10`（应正常渲染 plan）。
   - 非法：`model.target_backend=foo`（应在 `validate_resolved_run` 的 `_validate_training_topology` 报 `ValueError`）。
   记录两条命令的实际输出，并指出非法那条是被 [planning.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py) 里哪一道校验拦截的。
3. **画出角色双绑定**：结合 [cli.py:246-263](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L246-L263)，说明为什么 `bind_run` 在一次 `train` 里会被调用**两次**（第一次在 `resolve_run` 内对完整配置、第二次在 worker 分支对 role 投影后的配置），而 `registry.resolve` 只被调用**一次**。

**交付一张结论表**：

| 环节 | 函数 | 文件:行 | 输入 | 输出 | 是否查表 |
| --- | --- | --- | --- | --- | --- |
| 翻译 strategy | `resolve_run`→`registry.resolve` | composition.py / registry.py | strategy 字符串 | `AlgorithmRegistration` | 是（唯一一次） |
| 完整配置校验 | `bind_run`→`validate_resolved_run` | composition.py / planning.py | (cfg, algorithm) | `ResolvedRun` | 否 |
| role 投影后重校验 | `bind_run` | cli.py + composition.py | (role_config, algorithm) | `ResolvedRun` | 否 |
| 装配可执行 run | `build_application_run`→`build_training_run` | composition.py / assembly.py | `ResolvedRun` | `TrainingRun` | 否 |

**预期结果**：你能用一句话总结 SpecForge 的组合根哲学——「字符串 strategy 只被读一次，翻译成一个不可变的 `AlgorithmRegistration`，之后配置、校验、装配、启动全部围着这个对象转，任何下游都不再查名」。

**运行结果**：`--plan` 部分待本地验证（需先按 u1-l2 装好环境）；源码追踪部分现在即可完成。

## 6. 本讲小结

- **组合根唯一**：`application/composition.py` 是全项目唯一「把配置绑定到算法」的地方；外部统一从 `specforge.application` 导入 6 个公开名字。
- **ResolvedRun 是产物**：一个 frozen 值对象，把「一份已校验配置」和「它对应的那一个算法注册」捆在一起，构造后只读不可改。
- **AlgorithmRegistration 两半**：`spec` 是纯值契约（`_assert_pure_value` 保证不含可执行对象，校验层只读它），`providers` 是可执行端口（只有组合根/装配层触碰）。
- **resolve_run / bind_run 分工**：`resolve_run` 按 `training.strategy` 查表（唯一一次翻译）+ 校验；`bind_run` 跳过查表、只重做校验，服务于 role 投影后的「算法不变、配置变」场景。
- **validate_resolved_run 六道校验**：算法感知地检查 feature provider / draft options / capabilities / training topology / vocab mapping，把「方法 × 拓扑」的不支持组合 fail-fast 拦截在训练之前。
- **build_application_run 衔接装配**：双入口（`Config | ResolvedRun`）归一后，把 `resolved.config` + `resolved.algorithm` 交给 `training.assembly.build_training_run`，返回带 `.run()` 的可执行 `TrainingRun`；装配层绝不自己查表。

## 7. 下一步学习建议

本讲把「入口启动」与「算法解析」两条线收口到了组合根。接下来建议：

- **进入训练装配细节**：本讲止步于 `build_training_run` 的入口。真正的「造草稿模型、造 tokenizer、装优化器、装数据加载」全部在 `specforge/training/assembly.py`，那是 **u6-l1 训练装配 assembly** 的主题，读完它你就能把「组合根 → 装配层」这条链补全。
- **深入算法契约与 providers**：本讲只把 `AlgorithmSpec`/`providers` 当成「两半」来用。若想理解契约到底声明了哪些字段、providers 提供了哪些端口，读 **u4-l1 算法契约 contracts** 与 **u4-l2 算法注册表 registry 与 builtin**。
- **自己加一个算法**：组合根的简洁性意味着「新增算法」的落点很清晰——声明 `AlgorithmSpec`、实现 `AlgorithmProviders`、在 `builtin.py` 里注册。这是 **u10-l2 新增一个训练算法** 的端到端实践。
- **离线特征准备**：本讲提到但未展开的 `resolve_offline_capture`（[composition.py:58-130](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L58-L130)）是离线特征准备的组合根入口，可与 **u5-l3 离线特征生成** 配套阅读。
