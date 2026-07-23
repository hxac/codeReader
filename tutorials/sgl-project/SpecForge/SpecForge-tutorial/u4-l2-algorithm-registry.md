# 算法注册表 registry 与 builtin

## 1. 本讲目标

上一讲（u4-l1）我们读懂了 `AlgorithmSpec`：一份**只有静态约束、不含任何可执行对象**的纯数据契约。但训练时真正要构造草稿模型、要跑前向反向、要读离线特征——这些「会动的代码」在哪里？本讲就回答这个问题。

学完本讲你应当能够：

- 说清 `AlgorithmRegistration` 的「双半结构」：一半是纯契约 `spec`，另一半是可执行 `providers`，以及为什么要把它们绑在同一个对象里。
- 解释 `AlgorithmRegistry` 为什么是**不可变**的，它在构造时做了哪三道校验（类型、去重、排序）。
- 掌握 `registry.resolve(name)` 的查表行为与失败语义，并知道组合根如何把它接进真实训练链路。
- 说出 `builtin_algorithm_registry()` 注册了哪些算法名，以及它为什么是一个「工厂函数」而不是模块级全局变量。

## 2. 前置知识

本讲承接 u4-l1，默认你已经了解：

- **纯契约 `AlgorithmSpec`**：用 `name` / `draft` / `feature_contracts` / `capabilities` 四件套描述一个算法「需要什么」，但不含模型类、不含工厂函数。
- **`_assert_pure_value`**：在 `AlgorithmSpec` 构造末尾递归扫描，拒绝任何 `callable` 或 `type`，保证契约可在解析期轻量处理、可哈希、可序列化。

如果你还没读过 contracts.py，建议先回到 u4-l1 把 `AlgorithmSpec` 与 `FeatureContract` 过一遍。

本讲还会用到几个 Python 基础概念，先一句话解释：

- **`dataclass(frozen=True)`**：用 `@dataclass` 自动生成 `__init__` 等方法，`frozen=True` 让实例创建后字段不可修改（任何赋值都会抛异常），相当于「值对象」。
- **`object.__setattr__(self, ...)`**：在 `frozen=True` 的 dataclass 内部「破例」写一次字段的官方逃生口，用于 `__post_init__` 里做归一化后再落盘。
- **循环导入（circular import）**：模块 A 导入 B、B 又导入 A 会让 Python 启动失败，常见解法是把依赖方向设计成单向的。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [specforge/algorithms/registry.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py) | 定义 `AlgorithmRegistration`（双半绑定）与 `AlgorithmRegistry`（不可变目录）。这是最底层叶子模块，只依赖 contracts。 |
| [specforge/algorithms/builtin.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py) | `builtin_algorithm_registry()` 把全部内置算法装配成一个目录并返回。 |
| [specforge/algorithms/common/providers.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py) | 定义可执行的 `AlgorithmProviders` 及把 spec+providers 绑定并做 parity 校验的 `make_registration`。 |
| [specforge/algorithms/contracts.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py) | u4-l1 主角，提供 `AlgorithmSpec`。本讲只引用不重复讲。 |
| [specforge/algorithms/dflash/providers.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py) | 以 DFlash 为例，展示一个算法如何产出自己的 `AlgorithmSpec` 与 `AlgorithmProviders`。 |
| [specforge/application/composition.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py) | 组合根 `resolve_run`，本讲末尾点出 registry 在真实链路里的接线位置。 |

---

## 4. 核心概念与源码讲解

### 4.1 AlgorithmRegistration：纯 spec 与可执行 providers 的双半绑定

#### 4.1.1 概念说明

u4-l1 留了一个悬念：`AlgorithmSpec` 故意「只有值、没有代码」，那真正会构造模型、会跑训练步的代码放哪？答案是在**另一个对象**里——`providers`。

`AlgorithmRegistration` 就是把这两半**绑成同一个对象**的一次性结果：

- `spec`：纯契约（`AlgorithmSpec`），描述「这个算法需要什么、兼容什么」。
- `providers`：可执行端口（`AlgorithmProviders`），描述「这个算法怎么干」。

为什么非要绑在一起，而不是维护「spec 注册表」和「providers 注册表」两张表？源码注释一句话点破了动机：

> Keeping the two halves in one registration avoids parallel spec/provider registries. Planning reads `spec`; the composition root alone reads `providers`.

——两张表迟早会漂移（spec 改了 providers 忘改），绑在一起再在构造时做一次 parity 校验，就能在**装配那一刻**就发现不一致，而不是训练跑到一半才崩。

#### 4.1.2 核心流程

一次「绑定」的流程：

1. 某算法模块（如 dflash）分别产出 `AlgorithmSpec` 与 `AlgorithmProviders`。
2. 调用 `make_registration(spec, providers)`，在内部做**契约与实现的一致性校验**（feature contract 的 `(mode, modality)` 键集合必须与 provider 的 offline/streaming 键集合完全相等）。
3. 校验通过后，返回一个 `AlgorithmRegistration(spec=..., providers=...)`。
4. `AlgorithmRegistration.__post_init__` 再做一次「名字一致性」兜底校验。

也就是说，绑定这件事**经过两道校验**：`make_registration` 校验「能力对得上」，`AlgorithmRegistration.__post_init__` 校验「名字对得上」。

#### 4.1.3 源码精读

`AlgorithmRegistration` 本身非常薄，就是一个 frozen dataclass 加一个 `__post_init__`：

这是双半字段与校验，[specforge/algorithms/registry.py:11-37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L11-L37)：

- `spec: AlgorithmSpec`、`providers: object` 是两个字段。
- 注意 `providers` 的类型标注是 **`object`**，而不是 `AlgorithmProviders`。这是刻意为之：`registry.py` 只依赖 `contracts.py`，而 `providers.py` 反过来要 import `registry.py` 里的 `AlgorithmRegistration`。如果把 `providers` 标成 `AlgorithmProviders`，`registry.py` 就得 import `providers.py`，形成循环导入。标注成 `object` 让 registry 保持为最底层叶子模块。
- `__post_init__` 里读 `providers.algorithm_name` 并要求它等于 `spec.name`，这是「名字一致性」兜底。
- `name` 是个 property，直接委托给 `spec.name`——所以 `registration.name` 与 `registration.spec.name` 永远相同。

真正的「能力一致性」校验在 `make_registration`，[specforge/algorithms/common/providers.py:640-672](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L640-L672)。它检查：算法名一致、草稿架构在 `compatible_architectures` 内、以及最关键的一条——契约键集合与 provider 键集合必须**完全相等**：

\[
\{(mode_c, modality_c) \mid c \in \text{spec.feature\_contracts}\}
\;=\;
\{(\text{OFFLINE}, p.\text{modality}) \mid p \in \text{providers.offline}\}
\cup
\{(\text{STREAMING}, p.\text{modality}) \mid p \in \text{providers.server\_streaming}\}
\]

等式不成立就抛 `ValueError`。这保证「声明需要的特征」与「实际提供的读取器/collator」永远一一对应，不会出现「契约说要 offline text，但 provider 忘了写离线读取器」这种漂移。

#### 4.1.4 代码实践

**实践目标**：确认 `AlgorithmRegistration` 的双半字段与名字一致性校验。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [specforge/algorithms/registry.py:23-33](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L23-L33)，找到 `__post_init__`。
2. 想象有人手写了一个 `AlgorithmRegistration(spec=<name='dflash' 的 spec>, providers=<algorithm_name='eagle3' 的 providers>)`，追踪会抛什么异常、在哪一行抛。

**需要观察的现象**：`provider_name != self.spec.name` 时抛 `ValueError`，错误信息里同时打印两个名字方便定位。

**预期结果**：双半必须在名字上一致；`registration.name` 永远等于 `registration.spec.name`。

**待本地验证**：若你已装好环境，可在 Python 里 `from specforge.algorithms.registry import AlgorithmRegistration` 后构造一个非法实例，观察报错文本（构造合法实例需要先造 spec 与 providers，较繁琐，阅读源码即可）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AlgorithmRegistration.providers` 的类型标注是 `object` 而不是 `AlgorithmProviders`？

> **答**：为了避免循环导入。`registry.py` 是最底层叶子模块（只 import contracts），而 `providers.py` 需要反向 import `registry.AlgorithmRegistration`。标成 `object` 让 registry 不必感知 providers 模块，依赖方向保持单向。运行时实际传入的仍是 `AlgorithmProviders` 实例，`__post_init__` 通过 `getattr(providers, 'algorithm_name')` 鸭子式读取。

**练习 2**：`registration.name` 与 `registration.spec.name` 是什么关系？

> **答**：永远相等。`name` 是一个 property，实现就是 `return self.spec.name`，没有独立存储，故不可能漂移。

---

### 4.2 AlgorithmRegistry：不可变目录的构造、去重与排序

#### 4.2.1 概念说明

有了 `AlgorithmRegistration`，还需要一个「目录」把它们装起来供按名查询——这就是 `AlgorithmRegistry`。它的关键性质是**不可变（immutable）**：一旦构造完成，既不能改也不能加（「加」会返回一个**新的** registry）。

为什么强调不可变？因为整个进程里算法目录应当是**单一事实来源**：组合根查到的 `AlgorithmRegistration` 在一次 run 内不会变，下游装配层据此做静态决策。如果目录可变，今天查 `dflash` 得到 A、明天改成 B，校验与装配就会脱节。

#### 4.2.2 核心流程

`AlgorithmRegistry` 是 `frozen=True` 且 `init=False` 的 dataclass——关闭自动 `__init__`，自己手写一个，目的是在落盘前做三道校验：

1. **类型校验**：每个元素必须是 `AlgorithmRegistration`，否则收集所有非法类型名一次性报 `TypeError`。
2. **去重校验**：按 `name` 统计出现次数，任何出现超过一次的名字都算重复，报 `ValueError`。
3. **排序落盘**：通过 `object.__setattr__`（frozen dataclass 的唯一写入逃生口）把元组按 `name` 排序后存入 `_registrations`。

排序带来一个重要副作用：`names` 是**确定性有序**的——无论你以什么顺序传入注册项，最终 `names` 永远是字典序。这让 `--plan` 预览、错误信息、测试输出都稳定可复现。

#### 4.2.3 源码精读

构造与三道校验，[specforge/algorithms/registry.py:40-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L40-L68)：

- `@dataclass(frozen=True, init=False)`：冻结 + 关闭自动构造。
- 第 50-59 行：把输入拍成 tuple，挑出非 `AlgorithmRegistration` 的元素，若有则报 `TypeError`（一次性收集所有非法类型名）。
- 第 60-63 行：去重。用集合推导找出出现次数大于 1 的名字：

\[
\text{duplicates} = \{\, n \in \text{names} \mid \text{names.count}(n) > 1 \,\}
\]

- 第 64-68 行：`object.__setattr__` 把 `sorted(resolved, key=...)` 的结果写入 `_registrations`。排序键是 `registration.name`。

对外只读视图与唯一「衍生」方法，[specforge/algorithms/registry.py:70-90](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L70-L90)：

- `names` / `registrations` 都是 property，返回 tuple 的拷贝视图，无法被外部改写。
- `with_registration`：唯一「加法」入口，但它 `return AlgorithmRegistry((*self._registrations, registration))`——构造一个**全新的** registry，原对象不变。这是不可变数据结构的标准手法（persistent structure）。

#### 4.2.4 代码实践

**实践目标**：验证「构造即校验」与「不可变」。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读 [specforge/algorithms/registry.py:46-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L46-L68)。
2. 回答：如果同一个名字被注册两次，异常在哪一行抛？错误信息长什么样？
3. （可选运行）装好环境后执行下面这条命令，观察它**不会**抛异常，并思考为什么 `with_registration` 不违反「不可变」：

```bash
python -c "from specforge.algorithms.builtin import builtin_algorithm_registry as b; r=b(); print(type(r).__name__, 'frozen=', getattr(r.__class__.__dataclass_params__,'frozen',None))"
```

**需要观察的现象**：重复注册抛 `ValueError: duplicate algorithm registrations: [...]`；`frozen=True`。

**预期结果**：`AlgorithmRegistry frozen= True`。

**待本地验证**：上述 `python -c` 是否能在你的环境直接跑通取决于 `specforge` 顶层 import 是否拉起重依赖；registry 本身不 import torch，但包入口可能不同——若报导入错误属正常，阅读源码即可确认 `frozen=True`。

#### 4.2.5 小练习与答案

**练习 1**：`AlgorithmRegistry` 是 `frozen=True` 的，那 `with_registration` 是怎么「加」新算法的？它破坏了不可变性吗？

> **答**：没有破坏。`with_registration` 返回一个**新建的** `AlgorithmRegistry`（用 `(*self._registrations, registration)` 作参数重新走一遍 `__init__`），原对象原封不动。这是不可变数据结构的常见模式：每次「修改」都产生新实例。

**练习 2**：传入顺序是 `(eagle3, peagle, dflash, domino, dspark)`，最终 `registry.names` 的顺序是什么？

> **答**：字典序：`('domino', 'dflash', 'dspark', 'eagle3', 'peagle')`。因为构造时按 `name` 排序后再落盘。

---

### 4.3 registry.resolve：按 strategy 名查表

#### 4.3.1 概念说明

训练配置里的 `training.strategy` 只是一个字符串（如 `"dflash"`）。要把这个字符串变成「真正能驱动训练的算法对象」，靠的就是 `registry.resolve(name)`：按名在目录里查到那条 `AlgorithmRegistration`，把它交给组合根。

查表的失败语义特别讲究：未命中时**不是**静默返回 `None`，而是抛 `KeyError`，并且错误信息里**列出全部已注册算法名**——这样用户拼错名字（比如写成 `"DFlash"` 或 `"eagle"`）时能立刻看到所有合法选项。

#### 4.3.2 核心流程

1. 组合根 `resolve_run(cfg)` 从配置取出字符串 `cfg.training.strategy`。
2. 调 `registry.resolve(name)`，线性扫描 `_registrations`。
3. 命中 → 返回对应 `AlgorithmRegistration`；未命中 → 抛 `KeyError`。
4. 组合根把 `KeyError` 捕获并转成 `ValueError`（让错误统一走配置校验的语义）。

为什么是线性扫描而不是用 `dict`？因为目录里只有个位数算法（当前 5 个），O(n) 扫描的代价可忽略，而维护一个额外的 `name → registration` 字典反而要在不可变结构里多存一份冗余、多一道同步成本。简单优先。

#### 4.3.3 源码精读

`resolve` 的实现，[specforge/algorithms/registry.py:78-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L78-L84)：

- 一个 `for` 循环比对 `registration.name == name`，命中即返回。
- 未命中抛 `KeyError`，信息里带 `list(self.names)`——这就是「拼写错误的友好提示」来源。

组合根里的接线，[specforge/application/composition.py:40-55](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L40-L55)：

- 若调用方没传 `registry`，就 `from ...builtin import builtin_algorithm_registry; registry = builtin_algorithm_registry()`（惰性导入，避免启动期就加载内置算法）。
- `try: algorithm = registry.resolve(cfg.training.strategy) except KeyError as exc: raise ValueError(str(exc)) from exc`——把「未知算法」从 `KeyError` 翻译成配置校验常用的 `ValueError`。
- 拿到 `algorithm`（一个 `AlgorithmRegistration`）后，交给 `bind_run(cfg, algorithm)` 做后续六道校验（u3-l4 已讲）。同一个 registry 还服务于 `resolve_offline_capture`（[composition.py:58-72](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L58-L72)），即离线特征准备命令也走同一张表——保证「训练」与「离线 capture」对算法的理解永远一致。

#### 4.3.4 代码实践

**实践目标**：体验 `resolve` 的命中与未命中两条路径。

**操作步骤**（源码阅读型为主）：

1. 阅读 [specforge/algorithms/registry.py:78-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L78-L84) 与 [composition.py:50-53](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L50-L53)。
2. 追踪：当 `training.strategy='DFlash'`（大写，非法）时，异常类型在 `resolve` 内是 `KeyError`，到了 `resolve_run` 外部变成 `ValueError`。

**需要观察的现象**：未命中的错误信息形如 `unknown algorithm 'DFlash'; registered algorithms: ['domino', 'dflash', 'dspark', 'eagle3', 'peagle']`。

**预期结果**：用户拼错算法名时，能在第一时间看到全部合法名（且按字典序排列，因为目录构造时已排序）。

**待本地验证**：可在一个无效 YAML 上跑 `specforge train --config bad.yaml --plan`，观察终端报错文本是否如上（`--plan` 不占 GPU，见 u2-l3）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `resolve` 用线性扫描而不是维护一个内部 `dict`？

> **答**：目录规模极小（当前 5 个算法），O(n) 扫描成本可忽略；额外维护 `dict` 会让不可变结构多一份冗余字段与同步成本。简单优先。

**练习 2**：`resolve_run` 为什么把 `KeyError` 转成 `ValueError`？

> **答**：统一错误语义。`KeyError` 在 Python 里通常表示「字典查不到键」，而在这里它本质是「用户配置了一个非法/未知的 `training.strategy`」，属于配置校验错误，用 `ValueError` 更贴切，也便于上游统一捕获配置类异常。

---

### 4.4 builtin_algorithm_registry：内置算法的唯一目录

#### 4.4.1 概念说明

`builtin_algorithm_registry()` 是 SpecForge 进程里默认的算法目录来源。它把团队内置的全部算法（当前 5 种）装配成一个 `AlgorithmRegistry` 返回。

注意它是一个**函数**（带括号调用），而不是一个模块级全局变量。源码注释直说原因：

> Return a fresh immutable catalog without module-level mutation.

——即「返回一个全新的不可变目录，不做模块级修改」。如果做成模块级单例，任何测试或扩展若想临时替换某个算法就得修改全局状态，容易互相污染；做成函数则每次调用都拿到一个干净的新目录，组合根还可以接受外部传入的 `registry` 参数做替换（见 `resolve_run(cfg, registry=None)`）。

#### 4.4.2 核心流程

每个内置算法在自己的 `<算法>/providers.py` 里暴露一个 `create_registration()`，它内部调 `make_registration(algorithm_spec(), algorithm_providers())` 产出一条 `AlgorithmRegistration`。`builtin_algorithm_registry()` 把这些「构造器」**依次调用**，把结果喂给 `AlgorithmRegistry`：

1. 导入 5 个算法的 `create_registration`（导入时各重命名为短名 `eagle3` / `peagle` / `dflash` / `domino` / `dspark`）。
2. 调用 `AlgorithmRegistry((eagle3(), peagle(), dflash(), domino(), dspark()))`。
3. `AlgorithmRegistry.__init__` 自动做类型/去重/排序校验，返回最终目录。

以 DFlash 为例，它的两半分别由这两个函数产出：`algorithm_spec()`（[dflash/providers.py:133-162](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L133-L162)）与 `algorithm_providers()`（[dflash/providers.py:165-228](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L165-L228)），最后由 `create_registration`（[dflash/providers.py:231-232](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L231-L232)）绑成一条注册项。

#### 4.4.3 源码精读

builtin 的全部内容，[specforge/algorithms/builtin.py:5-16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py#L5-L16)：

- 第 5-9 行：从 5 个算法包各导入 `create_registration`，并用 `as` 重命名为短名（注意这些短名现在指向**函数**，不是字符串）。
- 第 13-16 行：`builtin_algorithm_registry()` 一行返回 `AlgorithmRegistry((eagle3(), peagle(), dflash(), domino(), dspark()))`——调用每个函数得到 `AlgorithmRegistration`，交给目录构造器。

这里有一个精妙之处：`create_registration()` 调用时只构造了一堆 dataclass 和**函数引用**（如 `build_draft`、`build_step`），并没有真正 import torch 或 transformers——那些重导入被故意推迟到这些函数**被调用时**才发生（见 [common/providers.py:348-354](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L348-L354) 的 `ModelProvider` 注释「Keeping concrete model imports inside hook functions lets the immutable registry resolve without importing Torch or Transformers」）。所以构造 registry 是**轻量**的，`--plan` 这种不需要 GPU 的预览也能安全完成算法解析。

#### 4.4.4 代码实践

**实践目标**：列出当前注册的全部算法名，并理解 `resolve('dflash')` 返回对象的双半内容。

**操作步骤**（可运行，无需 GPU；若环境导入失败则转为源码阅读）：

1. 执行下面命令打印内置算法名：

```bash
python -c "from specforge.algorithms.builtin import builtin_algorithm_registry as b; print(b().names)"
```

2. 对照源码 [builtin.py:13-16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py#L13-L16) 与 [dflash/providers.py:133-228](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L133-L228)，填写下表。

**需要观察的现象**：

- 命令输出为元组：`('domino', 'dflash', 'dspark', 'eagle3', 'peagle')`（字典序，与传入顺序无关）。
- `b().resolve('dflash')` 返回一个 `AlgorithmRegistration`，其 `.spec` 与 `.providers` 分别指向：

| 字段 | 指向内容 | 出处 |
| --- | --- | --- |
| `.name` | `'dflash'`（property 委托 spec.name） | registry.py:35-37 |
| `.spec` | DFlash 的 `AlgorithmSpec`：`name='dflash'`，`draft` 兼容架构 `{'DFlashDraftModel'}`，`feature_contracts` 含一条 OFFLINE text（required=`{input_ids, loss_mask, hidden_states}`，带 `OfflineStorageContract`）和一条 STREAMING text（不带 storage），`capabilities` 的 `attention_backends={'eager','sdpa','flex_attention'}` | dflash/providers.py:133-162 |
| `.providers` | DFlash 的 `AlgorithmProviders`：`algorithm_name='dflash'`，含 `step`（`StepProvider`，`build` 指向构造 `DFlashTrainStrategy` 的 `build_step`）、`model`（`ModelProvider`，`draft_config.architecture='DFlashDraftModel'`，并带若干懒加载的 `build_draft`/`build_training_model` 等钩子）、`offline`（一条文本 `OfflineDataProvider`）、`server_streaming`（一条文本 `ServerStreamingProvider`） | dflash/providers.py:165-228 |

**预期结果**：`spec` 是「纯数据」（可哈希、无 callable），`providers` 是「可执行」（满是函数引用）；两者经 `make_registration` 校验过 `(mode, modality)` 键集合一致后才绑在一起。

**待本地验证**：上面的 `python -c` 在纯 CPU、未装 GPU 版 torch 的环境里能否跑通取决于 `specforge` 包入口是否触发重导入；若报错，直接对照源码填表即可——`names` 的取值由 [builtin.py:16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py#L16) 决定，与运行无关。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `builtin_algorithm_registry` 是函数而不是模块级常量（如 `BUILTIN_REGISTRY = AlgorithmRegistry(...)`）？

> **答**：为了「不做模块级修改」。函数每次返回一个全新的不可变目录，测试或扩展若想临时替换算法，可构造自己的 `AlgorithmRegistry` 传给 `resolve_run(cfg, registry=...)`，而不必污染全局状态；同时也避免 import 副作用——目录只在真正被调用时才构造。

**练习 2**：调用 `builtin_algorithm_registry()` 时，会 import torch 吗？

> **答**：不会。各 provider 把 torch/transformers 的真实导入放在钩子函数（如 `build_draft`、`build_step`）**内部**，构造 registry 时只存函数引用。这使得 `--plan` 等无 GPU 场景也能安全完成算法解析。这正是 `AlgorithmProviders` 用「钩子函数 + 懒导入」而非直接持有模型类的原因。

---

## 5. 综合实践

把本讲四条线索串起来，完成下面这个「从字符串到注册项」的追踪任务。

**任务**：假设用户写了一份 YAML，其中 `training.strategy: dflash`。请画出从「这个字符串」到「拿到可执行 providers」的完整链路，并回答三个问题。

**步骤**：

1. 在源码中标注这条链路的五个关键点（文件 + 行号）：
   - 配置里的字符串 `cfg.training.strategy`；
   - 组合根惰性获取目录：[composition.py:46-49](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L46-L49)；
   - 按名查表：[registry.py:78-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L78-L84)；
   - builtin 目录如何构造：[builtin.py:13-16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py#L13-L16)；
   - 单算法的两半如何绑定：[dflash/providers.py:231-232](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L231-L232) 调 [common/providers.py:640-751](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L640-L751)。
2. 回答：
   - 链路中一共经过了几道校验？分别校验什么？（提示：`make_registration` 的 parity、`AlgorithmRegistration.__post_init__` 的名字一致、`AlgorithmRegistry.__init__` 的类型/去重。）
   - 若把 YAML 改成 `training.strategy: DFlash`（大写），最早在哪一行抛错？异常类型在 `resolve` 内与在 `resolve_run` 外分别是什么？
   - 整条链路有没有任何一处真正 import 了 torch？为什么这对 `--plan` 很重要？

**预期结果**：你能用一句话概括——`training.strategy` 字符串经组合根交给默认 builtin 目录，`resolve` 按名查到那条「双半绑定」好的 `AlgorithmRegistration`，整个解析过程轻量、可复现、不碰 GPU。

## 6. 本讲小结

- `AlgorithmRegistration` 是「双半绑定」：`spec`（纯契约，u4-l1）+ `providers`（可执行端口），绑在一起避免维护两张会漂移的表。
- `providers` 字段类型刻意标为 `object`，让 `registry.py` 保持为只依赖 contracts 的叶子模块，避免循环导入。
- 绑定经过两道校验：`make_registration` 校验契约/provider 的 `(mode, modality)` 键集合相等，`AlgorithmRegistration.__post_init__` 校验名字一致。
- `AlgorithmRegistry` 不可变（`frozen=True`），构造时做类型校验、按名去重、按名排序；`with_registration` 返回新对象而非原地修改。
- `registry.resolve(name)` 线性扫描，未命中抛 `KeyError` 并列出全部合法算法名；组合根再把它转成 `ValueError`。
- `builtin_algorithm_registry()` 是工厂函数（非模块级单例），注册 5 个算法（eagle3/peagle/dflash/domino/dspark），构造过程懒加载重依赖、不 import torch。

## 7. 下一步学习建议

- 下一讲 **u4-l3 算法 providers 与扩展端口** 会展开本讲里的「可执行半边」`AlgorithmProviders`：逐个讲清 `StepProvider` / `ModelProvider` / `OfflineDataProvider` / `ServerStreamingProvider` 这些端口各自提供什么钩子、`resolve_capture_layers` 如何决定捕获哪些目标层。
- 如果你想看「双半绑定」之后算法如何被装配进训练，可先跳读 [specforge/application/composition.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py) 的 `bind_run` 与 `build_application_run`（u3-l4 已讲组合根全貌）。
- 想了解「新增一个算法」需要改哪里，直接看 [specforge/algorithms/dflash/providers.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py) 这一个文件——它就是 u10-l2「新增一个训练算法」的范本。
