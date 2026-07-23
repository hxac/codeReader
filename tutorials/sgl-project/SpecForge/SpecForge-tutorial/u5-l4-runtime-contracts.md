# 跨平面契约与 SampleRef

## 1. 本讲目标

SpecForge 的运行时（`specforge/runtime/`）被刻意切成「控制面」和「数据面」两条通道：控制面负责调度、去重、租约、记账；数据面负责搬运大体积的隐藏状态张量。这两条通道之间靠什么对话？答案就是本讲的主角——一批**纯标准库**写出来的小数据类，叫做「跨平面契约（cross-plane contracts）」。

学完本讲，你应当能够：

1. 说清 `PromptTask`、`SampleRef`、`FeatureSpec`、`FeatureHandle`、`TrainBatch` 这五个记录各自的职责，以及**只有 `TrainBatch` 携带张量**这一铁律。
2. 理解 `SampleRef` 如何用 `feature_store_uri` + `feature_keys` 像指针一样「指向」一个样本的张量，而自己不持有任何张量。
3. 看懂 `assert_no_tensors` 的递归守卫机制，明白它为何能在不导入 `torch` 的前提下识别出张量。
4. 把「控制面只传元数据、张量只在数据面」这条不变量，与分布式训练的进程边界联系起来。

---

## 2. 前置知识

本讲是运行时部分的「契约层」，不涉及具体的训练算法，但需要你先建立两个直觉。

**直觉一：什么是「平面（plane）」？**

SpecForge 借用了网络/系统设计里「控制面 vs 数据面」的说法，类比家里的「信封」和「家具」：

| 平面 | 传输的东西 | 体积 | 类比 |
|---|---|---|---|
| 控制面（control plane） | 元数据：任务编号、样本指针、形状描述、租约令牌 | 很小（几百字节） | 信封上的收件地址 |
| 数据面（data plane） | 真正的张量：隐藏状态、token id、loss mask | 很大（MB 级） | 被搬运的家具本身 |

控制面像「快递单」，便宜、可以随时复制、可以跨进程序列化成 JSON；数据面像「货物」，笨重、要专门的仓库（`FeatureStore`）和卡车（Mooncake / RDMA / 共享盘）来搬。把它们分开的最大好处是：**调度逻辑可以又轻又快，不用每次都拖着几 MB 的张量走。**

**直觉二：承接 u3-l4 的「组合根」。**

[u3-l4](u3-l4-composition-root.md) 讲过，组合根 `build_application_run` 把配置、算法注册、训练器装配到一起，产出的是一个可执行的 `run`。当这个 `run` 真正跑起来后，进程内部就开始大量传递本讲要讲的这些小记录。所以本讲是组合根「装配好之后、各平面互相通信时用的语言」。你可以把本讲理解为：**u3-l4 造好了机器，本讲讲机器内部零件之间传递的「标准件规格」。**

如果你还没读过 [u3-l4](u3-l4-composition-root.md)，建议先读，因为本讲多次引用「跨平面不变量」这一概念，它在 u3-l4 与 u1-l5 中已被引入。

---

## 3. 本讲源码地图

本讲围绕两个核心文件展开，并辅以三个「真实调用方」文件来证明这些契约不是孤立的纸上设计。

| 文件 | 作用 | 本讲定位 |
|---|---|---|
| [specforge/runtime/contracts.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/contracts.py) | 定义全部跨平面记录与 `assert_no_tensors` 守卫 | **主角**，本讲全部内容的源头 |
| [specforge/runtime/CONTRACTS.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/CONTRACTS.md) | 这些契约的设计备忘与速查表 | **设计图纸**，含「记录一览表」 |
| [specforge/runtime/control_plane/controller.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py) | 控制面调度器，在每个记录入口调用 `assert_no_tensors` | 证明「守卫真的被用了」 |
| [specforge/runtime/data_plane/feature_store.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py) | 数据面仓库，`put` 张量后返回 `SampleRef` | 证明「指针是怎么造出来的」 |
| [specforge/runtime/data_plane/disagg_ingest.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disagg_ingest.py) | 分离式 producer 把 ref 集合序列化为 JSON manifest | 证明「元数据能跨进程」 |
| [tests/test_runtime/test_contracts.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_contracts.py) | 契约层单元测试 | 本讲代码实践的依据 |

> 提示：`contracts.py` 全文只 `import dataclasses` 和 `typing`，连 `torch` 都不真正导入（仅在 `TYPE_CHECKING` 下做类型注解）。这是刻意的——后面会解释为什么。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 控制面元数据记录**：`PromptTask` / `SampleRef` / `FeatureSpec` / `FeatureHandle`。
- **4.2 数据面唯一张量契约**：`TrainBatch`。
- **4.3 无张量不变量守卫**：`assert_no_tensors` 与 `_looks_like_tensor`。

### 4.1 控制面元数据记录：PromptTask / SampleRef / FeatureSpec / FeatureHandle

#### 4.1.1 概念说明

控制面的核心任务是「调度」：哪个 worker 该抓哪个 prompt、哪个样本已经被捕获、哪个样本还没被消费。要完成这些调度，控制面需要在各组件之间传递一些信息。但有一个硬约束：**这些信息必须很「轻」**，因为它们会被频繁复制、加锁、序列化、跨进程发送。如果把几 MB 的隐藏状态也塞进调度信息里，整个调度路径会被张量拖垮。

于是 SpecForge 设计了一组**只携带元数据（metadata）**的记录：

- `PromptTask`：一份「待捕获的 prompt」工作单元。它告诉 rollout worker「去抓哪段对话」，但只带文本/token id 列表，不带目标模型的隐藏状态（隐藏状态是 worker 抓出来之后才有的产物）。
- `FeatureSpec`：对「某一个命名张量」的形状/类型描述。注意是**描述**，不是张量本身——它只说「这个特征名叫 `target`，形状是 `(1, 8, 16)`，类型是 `bfloat16`」，让你提前知道要取多大的东西。
- `SampleRef`：本讲的「明星」。它是一个**指针**，指向「某个样本的全部特征」。它自己不持有任何张量，而是通过 `feature_store_uri`（特征仓库名址）+ `feature_keys`（每个特征在仓库里的位置键）来定位。就像图书馆的索书号：你知道书在哪，但你手里拿的只是一张纸条。
- `FeatureHandle`：从仓库「借出」张量时拿到的一张**租约令牌**。归还张量（`release`）时必须出示它。

这四个记录有一个共同点：都用 `@dataclass(frozen=True)` 声明为**不可变值对象**。不可变意味着它们可以安全地被多个线程/进程共享，不会被谁偷偷改掉。

#### 4.1.2 核心流程

一条样本在控制面里「从无到有」的生命周期：

```
1. ingest_prompts(原始对话)
        │  把每条对话包装成 PromptTask（元数据），入待处理队列
        ▼
2. lease_prompt_tasks(worker)
        │  worker 租借 PromptTask，去数据面抓隐藏状态
        ▼
3. store.put(tensors)            ← 数据面动作：张量进仓库
        │  仓库返回一个 SampleRef（指针，无张量）
        ▼
4. commit_samples(refs)
        │  控制面记账、去重，决定这个 ref 是否「新鲜」
        ▼
5. loader 取样本：store.get(ref)  ← 数据面动作：凭指针换张量
        │  返回 (tensors, FeatureHandle)
        ▼
6. store.release(handle)          ← 用完归还，仓库可能释放张量
```

关键在于：第 1、2、4 步走的是控制面，传递的全是元数据；只有第 3、5、6 步才真正碰张量，而且张量始终待在 `FeatureStore` 里，从不进入 controller 的内存。`SampleRef` 就是这两条平面之间的「接头暗号」。

#### 4.1.3 源码精读

先看 `PromptTask`，它是一份工作单元的元数据封装：

[specforge/runtime/contracts.py:45-59](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/contracts.py#L45-L59) —— `PromptTask` 用 `frozen=True` 冻结，字段全是标量或纯字典，`payload` 装的是对话文本或 token id 列表（注意：是 Python list，不是 tensor）。

注意 docstring 里那句「Metadata only」——这是这个记录的契约承诺。

接着看本讲的主角 `SampleRef`：

[specforge/runtime/contracts.py:80-100](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/contracts.py#L80-L100) —— `SampleRef` 的关键字段：

- `feature_store_uri`：特征仓库的名址（如 `mem://store42/s0?generation=3`）。
- `feature_keys`：一个字典，把特征名映射到仓库内部的位置键（如 `{"input_ids": "s0/input_ids", "target": "s0/target"}`）。
- `feature_specs`：每个特征的 `FeatureSpec`（形状/类型描述）。
- `schema_version`：模式版本号，加载器据此判断旧 ref 是否还能读。

docstring 明说：「Exactly one sample per ref」——**一个 ref 恒等于一个样本**，拼 batch 是 loader 的事，不烤进 ref 里。这个设计让 ref 可以独立去重、独立传输、独立记租约。

再看它「指向但不持有」张量的伴侣 `FeatureSpec`：

[specforge/runtime/contracts.py:62-77](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/contracts.py#L62-L77) —— `FeatureSpec` 只有 `shape`、`dtype`、`device_hint` 这些**描述性**字段，外加 `target_repr`（目标特征的表示方式，留待 u6 训练策略讲）和 `target_meta`（词表映射版本等）。它让你「提前知道张量长什么样」，但张量本体绝不随 spec 旅行。

那么这个「指针」到底是怎么造出来的？看数据面仓库的 `put` 方法返回 `SampleRef` 的那几行：

[specforge/runtime/data_plane/feature_store.py:346-361](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L346-L361) —— 张量 `staged` 已经存进 `self._mem` 之后，`put` 返回的 `SampleRef` 里：

- `feature_store_uri=_make_mem_uri(self.store_id, sample_id, gen)` 生成形如 `mem://<store>/<sample>?generation=<n>` 的名址；
- `feature_keys={k: f"{sample_id}/{k}" for k in staged}` 生成每个特征的定位键；
- `feature_specs=specs` 是形状/类型描述。

**注意：这个返回对象里没有任何一个字段引用了 `staged` 张量本身。** 张量留在仓库里，ref 只是张「提货单」。这就是「指向而不持有」的全部秘密。

`_make_mem_uri` 的实现也很短，可以印证名址的结构：

[specforge/runtime/data_plane/feature_store.py:97-101](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L97-L101) —— 把 `generation`（再生物代号）拼进 URI 查询串，用于检测「陈旧 ref 指向已被覆盖的样本」。

最后看 `FeatureHandle`，归还张量时的租约令牌：

[specforge/runtime/contracts.py:103-115](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/contracts.py#L103-L115) —— 只有 `sample_id`、`generation`、`lease_token` 三字段。`generation` 在样本每次重新物化时自增，这样一次「过期的归还」会变成安全空操作（no-op），不会误删已被新租约占用的张量。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲手追踪「指针是怎么造出来的、又怎么换回张量」。

1. **实践目标**：用一句话说清 `SampleRef` 如何在不持有张量的前提下指向一个样本，并验证你的理解。
2. **操作步骤**：
   - 打开 [feature_store.py:290-361](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L290-L361) 的 `put` 方法。
   - 找到张量被存进仓库的那一行（提示：`self._mem[sample_id] = staged`）。
   - 再找到构造 `SampleRef` 的 `return SampleRef(...)` 块，确认返回对象里**没有**任何一个字段等于 `staged`。
   - 接着打开 [feature_store.py:122-129](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L122-L129) 的 `get` 抽象方法签名，确认它「吃进 `SampleRef`、吐出 `(tensors, FeatureHandle)`」——指针换张量就发生在这里。
3. **需要观察的现象**：`put` 的入参里有张量，返回值里没有张量；`get` 的入参里没有张量（只有 ref），返回值里有张量。张量始终只活在 `FeatureStore` 内部。
4. **预期结果**：你能画出「张量在仓库里原地不动，ref 像提货单一样在控制面流转」的图，并指出 `feature_store_uri` 是仓库名址、`feature_keys` 是仓库内定位键。
5. 运行结果：待本地验证（本实践为源码阅读型，无需运行命令）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SampleRef` 要设计成「一个 ref 恒等于一个样本」，而不是直接装一个 batch？

> **参考答案**：因为去重、租约、传输、记账都以「单个样本」为粒度最自然。如果把 batch 烤进 ref，那么任何一个样本重复都得处理整个 batch，去重和恢复都会变复杂。拼 batch 是数据加载器（`FeatureDataLoader`）的职责，与控制面记账解耦。

**练习 2**：`FeatureHandle` 里的 `generation` 字段解决什么问题？

> **参考答案**：样本可能被重新物化（re-put），此时仓库里同名样本的张量已被覆盖。如果一张旧的租约令牌还能触发「归还即释放」，就会误删已被新租约占用的张量。`generation` 在每次重新物化时自增，旧令牌的 generation 对不上当前代，归还就变成安全的空操作。

**练习 3**：`FeatureSpec` 里有 `shape` 和 `dtype`，为什么还要单独存在、而不直接把张量塞进 `SampleRef`？

> **参考答案**：`FeatureSpec` 是「张量的身份证」，让消费方在不真正取张量的前提下就能预算内存、规划显存、校验 schema（比如 `estimated_bytes` 就是靠它估的）。把张量塞进 ref 会同时破坏「轻量调度」和「可序列化」两个目标。

---

### 4.2 数据面唯一张量契约：TrainBatch

#### 4.2.1 概念说明

控制面只传元数据，那张量最终在哪「露脸」？答案是 `TrainBatch`——**整个运行时里唯一允许携带张量的契约**。

`TrainBatch` 代表「一个已经物化、已经拼好 batch、可以直接喂给 trainer 的训练批」。它出现在训练器（trainer）/数据面这一侧，是 `FeatureDataLoader` 把若干个 `SampleRef` 换成真实张量、做完 collate 之后的产物。

为什么要专门立一个「唯一张量契约」？因为这是**边界标记**。只要你看到 `TrainBatch`，就知道「从这一刻起，我们进入张量世界了」；只要你看的不是 `TrainBatch`，就可以放心假设里面没有张量。这条规则让整个运行时的张量流动是**可追踪、可推理**的，而不是「到处都可能藏着张量」。

#### 4.2.2 核心流程

从 ref 到 batch 的物化流程：

```
若干 SampleRef（指针，无张量）
        │  FeatureDataLoader 调 store.get(ref) 取回张量
        ▼
Dict[str, Tensor] + collate（按策略拼 batch、做 target 投影等）
        │  打包成 TrainBatch
        ▼
TrainBatch(sample_ids, strategy, tensors, metadata)
        │  交给 TrainerCore / DraftTrainStrategy.forward_loss
        ▼
计算损失、反向传播
```

注意中间的 collate 步骤是**策略相关**的（不同算法对 target 的处理不同，详见 u6-l2 训练策略），但 `TrainBatch` 这个容器本身是策略无关的——它只承诺「装了一堆命名张量」。

#### 4.2.3 源码精读

看 `TrainBatch` 的定义：

[specforge/runtime/contracts.py:118-129](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/contracts.py#L118-L129) —— 注意三个细节：

1. 它是 `@dataclass`（**没有** `frozen=True`），因为张量在训练中会被原地改写（比如梯度累积），故意可变。
2. `tensors: Dict[str, "torch.Tensor"]` 是唯一装张量的字段，`"torch.Tensor"` 用字符串注解（forward reference），配合模块顶部的 `TYPE_CHECKING` 守卫。
3. docstring 明确写道：「This is the *only* contract that carries tensors, and only ever on the trainer / data-plane side.」

再看模块顶部为什么 `torch` 是「只用于类型检查」：

[specforge/runtime/contracts.py:28-29](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/contracts.py#L28-L29) —— `if TYPE_CHECKING: import torch`。这意味着运行时**根本不导入 torch**，所以 `contracts.py` 可以在没有 GPU、没有 torch 的纯 Python 环境里被 import 和单元测试。这也是为什么下一节的 `assert_no_tensors` 必须「不导入 torch 也能认出张量」。

最后，结合 CONTRACTS.md 的速查表，把五个记录的「是否带张量」一次看全：

[specforge/runtime/CONTRACTS.md:40-50](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/CONTRACTS.md#L40-L50) —— 表格里只有 `TrainBatch` 那一行标了 **yes (trainer side only)**，其余四行全是 no。这张表就是本节结论的权威出处。

#### 4.2.4 代码实践

这是一个**阅读测试型实践**，目标是借助官方测试加深对「唯一张量契约」的理解。

1. **实践目标**：确认 `TrainBatch` 是唯一被允许携带张量的记录，并理解为什么 `assert_no_tensors(batch)` 会抛错。
2. **操作步骤**：
   - 打开 [tests/test_runtime/test_contracts.py:97-107](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_contracts.py#L97-L107) 的 `test_trainbatch_holds_tensors`。
   - 阅读它构造的 `TrainBatch`：`tensors={"input_ids": torch.zeros(1, 8, dtype=torch.long)}`。
   - 注意最后两行：它先断言张量确实在 `batch.tensors` 里，**再**断言 `assert_no_tensors(batch)` 抛 `TypeError`。
3. **需要观察的现象**：同一个 `TrainBatch` 对象，既「合法地装有张量」，又「通不过无张量检查」。这两件事并不矛盾。
4. **预期结果**：你能解释——`assert_no_tensors` 是**控制面**的守卫，`TrainBatch` 属于**数据面**，控制面本来就不该碰到它；如果控制面真的收到了一个 `TrainBatch`，那就是边界被破坏了，理应报错。
5. 运行结果：待本地验证（可在装好 torch 的环境执行 `python -m pytest tests/test_runtime/test_contracts.py::TestContracts::test_trainbatch_holds_tensors -v` 观察通过）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TrainBatch` 不像其他四个记录那样 `frozen=True`？

> **参考答案**：因为训练过程中张量会被原地修改（如梯度累积、就地反传优化），而 `frozen=True` 会禁止字段重新赋值。控制面记录需要不可变是因为它们会被多方共享、需要可哈希可序列化；`TrainBatch` 只在 trainer 本地流转、生命周期短，可变更贴合实际。

**练习 2**：如果有人把一个 `TrainBatch` 传给了控制面的 `commit_samples`，会发生什么？

> **参考答案**：`commit_samples` 会对每条记录调用 `assert_no_tensors`（见 4.3 节），而 `TrainBatch` 里有张量，于是会抛 `TypeError`，拦截这次违规。这正是守卫的价值——把「张量不该出现在控制面」从口头约定变成运行时硬约束。

---

### 4.3 无张量不变量守卫：assert_no_tensors

#### 4.3.1 概念说明

前两节讲了规则：「控制面记录只带元数据，张量只在 `TrainBatch`」。但口头规则会被打破——有人可能不小心把一个张量塞进 `SampleRef.metadata` 这个 `Dict[str, Any]` 里（毕竟类型签名允许任意值）。SpecForge 不满足于「希望大家自觉」，而是写了一个运行时守卫 `assert_no_tensors`：**递归地扫描任意对象，一旦发现张量就立刻抛 `TypeError`**。

这个守卫有两个巧妙之处：

1. **不导入 torch/numpy 也能识别张量**。它用「鸭子类型（duck typing）」判断：看对象的类是否属于 `torch`/`numpy` 模块根，或者是否同时具有 `dtype`、`shape`、`device` 三个属性。这样 `contracts.py` 得以保持纯标准库。
2. **带路径面包屑（breadcrumb）**。报错信息会精确指出张量藏在哪个字段路径下，比如 `<root>.metadata['sneaky']`，方便定位。

#### 4.3.2 核心流程

`assert_no_tensors(obj)` 的判定逻辑（伪代码）：

```
def assert_no_tensors(obj, path="<root>"):
    if obj 是 None 或 标量(str/bytes/bool/int/float):
        return                          # 安全，直接放行
    if _looks_like_tensor(obj):         # 鸭子判定
        raise TypeError(f"tensor found at {path}")
    if obj 是 dataclass 实例:
        for 每个字段 f:
            if 值是 None 或 标量: continue      # 快速跳过，不递归
            assert_no_tensors(值, path=f"{path}.{f.name}")
    elif obj 是 dict:
        for k, v: 对每个非标量值递归 (path 加 [k])
    elif obj 是 list/tuple/set/frozenset:
        for i, v: 对每个非标量元素递归 (path 加 [i])
    else:
        return                          # 其它不透明元数据，放行
```

这里有一个关键的性能优化：**对全是标量的序列直接跳过递归**。因为 prompt 的 `payload` 里常常是几千个 token id 组成的 list，如果对每个 int 都递归一次，开销巨大。所以守卫先看「元素是不是标量」，是标量就 `continue`，只对非标量元素（比如嵌套的容器或张量）才深入。测试 `test_assert_no_tensors_skips_primitive_sequence_recursion` 专门验证：一个 4096 元素的纯整数 list 只会触发**一次**递归调用（对外层 list 本身），而不是 4096 次。

#### 4.3.3 源码精读

先看鸭子判定函数 `_looks_like_tensor`：

[specforge/runtime/contracts.py:145-153](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/contracts.py#L145-L153) —— 两条判定：

- 类的模块根是 `torch` 或 `numpy` → 直接判为张量；
- 否则看是否**同时**具有 `dtype`、`shape`、`device` 三个属性（普通 list/dict/str 都不会同时有这三个）。

接着看主守卫 `assert_no_tensors`：

[specforge/runtime/contracts.py:156-194](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/contracts.py#L156-L194) —— 重点看三段：

- 第 166-170 行：命中 `_looks_like_tensor` 就抛 `TypeError`，信息里带 `type=模块.类名` 和 `path`。
- 第 171-177 行：对 dataclass 实例，逐字段递归；标量字段直接 `continue`。
- 第 184-192 行：对 list/tuple/set/frozenset，逐元素递归，标量元素 `continue`。注释说明了「token-id 和 mask 序列占 prompt payload 的主体，逐元素递归太贵」。

那这个守卫到底在哪些地方被调用？看控制面 controller：

[specforge/runtime/control_plane/controller.py:65](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L65) —— `register_rollout_worker` 在记录 worker 信息前先 `assert_no_tensors(info)`；

[specforge/runtime/control_plane/controller.py:86](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L86) —— `ingest_prompts` 对每条原始 prompt 字典先做守卫（注释说：PromptTask 的每个字段要么来自已校验输入、要么由无张量默认值构造，所以一次边界检查就够了，不必把长 token list 走两遍）；

[specforge/runtime/control_plane/controller.py:184](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L184) —— `commit_samples` 对每条 `SampleRef` 调用 `assert_no_tensors(ref)`，注释直呼其名「online no-tensor guard」。

数据面也用——分离式 producer 在把 ref 集合写成 JSON manifest 之前先断言：

[specforge/runtime/data_plane/disagg_ingest.py:100](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disagg_ingest.py#L100) —— `write_ref_manifest` 在序列化前 `assert_no_tensors(refs)`，因为 manifest 是「控制面状态」，绝不能带张量。

最后看测试如何验证守卫能抓住「藏在 metadata 里的张量」：

[tests/test_runtime/test_contracts.py:79-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_contracts.py#L79-L84) —— `test_assert_no_tensors_catches_tensor_in_metadata`：用一个合法 ref，再用 `dataclasses.replace` 把 `metadata` 换成 `{"sneaky": torch.zeros(3)}`，断言守卫抛 `TypeError`。这证明：即使你把张量塞进看似自由的 `metadata` 字典，守卫也会钻进去抓出来。

#### 4.3.4 代码实践

这是一个**修改参数观察行为型实践**，目标是亲手触发守卫、读懂报错路径。

1. **实践目标**：构造一个把张量藏在 `metadata` 里的 `SampleRef`，触发 `assert_no_tensors`，并解读报错路径。
2. **操作步骤**（以下为**示例代码**，非项目原有代码）：
   ```python
   # 示例代码：演示守卫如何抓出藏在 metadata 里的张量
   import torch
   from specforge.runtime.contracts import SampleRef, FeatureSpec, assert_no_tensors

   ref = SampleRef(
       sample_id="s0",
       run_id="r0",
       source_task_id=None,
       feature_store_uri="mem://store/s0",
       feature_keys={"input_ids": "s0/input_ids"},
       feature_specs={"input_ids": FeatureSpec("input_ids", (1, 8), "int64")},
       strategy="eagle3",
       metadata={"sneaky": torch.zeros(3)},   # 故意夹带一个张量
   )

   try:
       assert_no_tensors(ref)
   except TypeError as e:
       print("被守卫拦截：", e)
   ```
3. **需要观察的现象**：守卫抛出 `TypeError`，信息里应包含形如 `tensor payload found at <root>.metadata['sneaky']` 的路径。
4. **预期结果**：报错路径精确指向 `<root>.metadata['sneaky']`，证明守卫递归钻进了 `metadata` 字典。
5. 运行结果：待本地验证（需装好 torch；本示例对应官方测试 `test_assert_no_tensors_catches_tensor_in_metadata`，可作为参照）。

#### 4.3.5 小练习与答案

**练习 1**：`assert_no_tensors` 为什么不直接 `import torch` 然后 `isinstance(obj, torch.Tensor)`？

> **参考答案**：因为 `contracts.py` 刻意保持纯标准库、运行时不导入 torch（torch 又重又依赖 CUDA）。直接 import torch 会让控制面契约无法在无 torch 环境下加载和单测，也违背了「控制面要轻」的设计初衷。鸭子判定让守卫在不依赖 torch 的前提下完成识别。

**练习 2**：守卫对「全是标量的长 list」（比如 4096 个 token id）做了什么优化？为什么需要这个优化？

> **参考答案**：它对 list/tuple 的每个元素先判断是否标量（None/str/bytes/bool/int/float），是标量就 `continue` 不递归。prompt 的 payload 里大量是 token-id 和 mask 序列，若对每个 int 都递归一次，开销与序列长度成正比、极其浪费。优化后，纯标量序列只触发对容器本身的一次调用。

**练习 3**：报错信息里的 `_path`（如 `<root>.metadata['sneaky']`）有什么实际价值？

> **参考答案**：它是一串「面包屑」，精确指出张量藏在对象的哪个嵌套位置。在真实运行里，`metadata` 可能层层嵌套，没有路径就只能知道「某处有张量」却找不到在哪；有了路径，定位和修复违规几乎是即时的。

---

## 5. 综合实践

把三个模块串起来，完成一个「跨平面追踪」小任务，帮助你建立全局图景。

**任务**：画出一张「一次样本从 prompt 到 batch」的完整流转图，并标注每一步**走的是控制面还是数据面、传递的是元数据还是张量**。

**建议步骤**：

1. 从控制面入口出发：阅读 [controller.py:79-110](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L79-L110) 的 `ingest_prompts`，标注「原始对话 → `PromptTask`（控制面/元数据）」，并指出守卫调用点在第 86 行。
2. 到数据面物化：阅读 [feature_store.py:290-361](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L290-L361) 的 `put`，标注「张量进仓库 → 返回 `SampleRef`（数据面动作，但返回的是控制面指针）」，圈出 `feature_store_uri` 与 `feature_keys` 两个字段。
3. 回控制面记账：阅读 [controller.py:175-199](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L175-L199) 的 `commit_samples`，标注「`SampleRef` 进账本（控制面/元数据）」，并指出第 184 行的守卫。
4. 再到数据面取回：阅读 `get` 的抽象签名 [feature_store.py:122-129](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L122-L129)，标注「`SampleRef` → `(tensors, FeatureHandle)`（数据面/张量）」。
5. 最终汇聚：标注「多个 ref 的张量经 collate → `TrainBatch`（数据面/张量，唯一带张量契约）」。

**自检问题**（答得出说明你掌握了本讲）：

- 在你画的图里，张量一共「跨过」了几次进程/平面边界？答案应当是**零次**——张量始终待在 `FeatureStore` 里，跨边界流动的只有元数据 ref。
- 如果 producer 与 consumer 是两个不同进程（分离式部署，见 u3-l3），producer 写给 consumer 的是什么？答案：是 `SampleRef` 集合序列化成的 JSON manifest（见 [disagg_ingest.py:94-109](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disagg_ingest.py#L94-L109)），不是张量。这正是下一小节强调的分布式意义。

**对分布式传输的意义**（呼应本讲实践任务）：`SampleRef` 只用 `feature_store_uri` + `feature_keys` 指向样本而不持有张量，这意味着控制面消息可以小到几百字节、可以序列化为 JSON、可以用任何轻量信道（普通 socket、文件、Redis）跨进程跨节点传递；而真正的几 MB 张量只通过专用的数据面后端（本地内存、共享盘、Mooncake/RDMA）搬运。如果 ref 里夹带了张量，那么每一次去重、每一次租约、每一次跨进程 manifest 序列化都得拖上整个张量，控制面的轻量与可序列化会被彻底破坏——`assert_no_tensors` 就是把这条边界焊死的护栏。

---

## 6. 本讲小结

- SpecForge 运行时被切成控制面（调度）与数据面（张量），二者靠一批**纯标准库**的跨平面契约对话。
- `PromptTask`、`SampleRef`、`FeatureSpec`、`FeatureHandle` 都是 `frozen=True` 的元数据记录，**不携带张量**；`SampleRef` 用 `feature_store_uri` + `feature_keys` 像指针一样指向样本。
- `TrainBatch` 是**唯一**允许携带张量的契约，故意不冻结，只活在 trainer/数据面一侧。
- `assert_no_tensors` 是运行时守卫，递归扫描 dataclass 字段、dict 值、序列元素，靠鸭子类型（`torch`/`numpy` 模块根或同时有 `dtype`+`shape`+`device`）识别张量，全程不导入 torch。
- 守卫被控制面 controller 在每个入口（`register_rollout_worker`、`ingest_prompts`、`commit_samples`）和数据面 manifest 写盘前调用，把「张量不得进入控制面」从约定变成硬约束。
- 这套设计的分布式意义：控制面消息轻到可序列化为 JSON 跨进程跨节点传递，大张量只走专用数据面后端——这是 SpecForge 在线分离式训练（u7）能高效协同的根基。

---

## 7. 下一步学习建议

本讲建立的是运行时的「契约语言」，接下来三条路可以选：

1. **向控制面深入 → [u7-l2 控制平面与元数据账本](u7-l2-control-plane.md)**：看 `DataFlowController` 如何用本讲的 `PromptTask`/`SampleRef` 做 prompt 租赁、提交去重，以及三种 metadata store（NoOp/InMemory/SQLite）如何持久化这些元数据。
2. **向数据面深入 → [u7-l3 数据平面 feature store 与传输](u7-l3-data-plane-stores.md)**：看 `FeatureStore` 的 `put`/`get`/`release`/`abort`/`gc` 生命周期，以及 `LocalFeatureStore`、shared_dir、Mooncake 三种后端如何实现同一契约。
3. **向训练侧深入 → [u6-l2 训练策略 DraftTrainStrategy](u6-l2-train-strategy.md)**：看 `TrainBatch.tensors` 里的 `target` 特征如何被各策略的 `forward_loss` 消费，以及 `FeatureSpec.target_repr`（`logits`/`pruned_logits`/`hidden_state`）如何决定 target 的投影方式。

建议优先读 u7-l2 和 u7-l3，它们会立刻用上本讲的全部概念。
