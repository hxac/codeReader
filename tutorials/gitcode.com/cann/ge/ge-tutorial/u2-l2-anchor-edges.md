# Anchor 锚点：数据边与控制边的表达

## 1. 本讲目标

上一讲（u2-l1）我们建立了 AscendIR 的「四层对象模型」`ComputeGraph → Node → OpDesc → GeTensorDesc`，并留下了一个关键结论：**AscendIR 不存在独立的 Edge（边）对象**，节点之间的连接完全由节点内嵌的「锚点（Anchor）」互相引用来表达。本讲就专门把这句话拆开讲透。

学完本讲，你应该能够：

1. 解释为什么 GE 选择「锚点」而非「独立边对象」来表达连边，并能说出锚点方案的几条收益。
2. 区分 **DataAnchor（数据锚点）** 与 **ControlAnchor（控制锚点）**，并说清它们各自的连接约束（单输入 vs 多扇出）。
3. 给定一个 `OutDataAnchor`，写出一段「遍历它所有下游消费节点」的伪代码，并知道何时该用裸指针版本以获得更好性能。

---

## 2. 前置知识

在进入源码前，先用通俗语言把三个基础概念讲清楚。

### 2.1 什么是「连边」

计算图里的算子节点本身是孤立的，要让它们组成一张能计算的图，就必须表达「A 的某个输出是 B 的某个输入」。这种「谁连到谁」的关系就是**边（edge）**。绝大多数图框架（比如 ONNX 用 `input` 字段、TensorFlow 用 `Edge` 对象）都会显式记录边。

GE 的不同之处在于：它**不新建一个 Edge 类**，而是把连边关系**内嵌到节点两端的「锚点」里**。锚点就是节点上的「接线端子」，端子之间互相记住对方，边就自然形成了。

### 2.2 数据流 vs 控制流

- **数据边（data edge）**：连接的是「真正流动的张量」。比如 `Conv` 的输出张量流入 `Relu` 的输入，这条边承载的是数据。
- **控制边（control edge）**：只表达「先后顺序依赖」，不承载张量数据。比如「B 必须在 A 执行完之后才能执行」，但 A 并不给 B 喂数据。在框架里通常用一条虚线表示。

GE 用两类锚点分别表达这两类边：`DataAnchor` 表达数据边，`ControlAnchor` 表达控制边。

### 2.3 「In/Out」与「扇入扇出」

每个节点都有「输入端子」和「输出端子」：

- **输入锚点（In*Anchor）**：数据的入口，给一个端口编号 `idx`（0、1、2……）。
- **输出锚点（Out*Anchor）**：数据的出口，同样有编号。

一个**输出**端子可以接很多个下游（一对多，即「扇出 fan-out」），但一个**输入数据**端子只能由一个上游供给（一对一）。这个约束是本讲的一个重点，后面会在源码里反复看到。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [inc/graph_metadef/graph/anchor.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/anchor.h) | 锚点类的**声明**：`Anchor` 抽象基类，及其四个子类 `InDataAnchor`/`OutDataAnchor`/`InControlAnchor`/`OutControlAnchor`。 |
| [graph_metadef/graph/normal_graph/anchor.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc) | 锚点类的**实现**：重点是 `AnchorImpl` 里存放对端引用的 `peer_anchors_`，以及 `LinkTo`/`LinkFrom`/`GetPeerXxx` 的真实逻辑。 |
| [inc/graph_metadef/graph/node.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h) | `Node` 类声明，提供「节点级」的锚点遍历便捷接口（如 `GetOutDataNodesPtr`）。 |
| [graph_metadef/graph/normal_graph/node.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/node.cc) | `Node::Init` 在节点创建时按 OpDesc 的输入/输出数量生成各类锚点。 |
| [graph_metadef/graph/utils/graph_utils.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/utils/graph_utils.cc) | `GraphUtils::AddEdge` 等便捷函数，是对 `LinkTo` 的一层薄封装，是实际写 Pass 时最常用的「加边」入口。 |

> 提示：GE 的核心类普遍采用 **Pimpl 模式**（声明类 + `Impl` 实现类），即「接口在头文件，实现在 `.cc` 的 `*Impl` 里」。`anchor.h` 里的 `Anchor` 只持有 `AnchorImplPtr impl_`，真正的 `peer_anchors_` 列表藏在 `anchor.cc` 的 `AnchorImpl` 中。阅读本讲时请记住这一点。

---

## 4. 核心概念与源码讲解

### 4.1 Anchor 抽象：连接关系为何内嵌于节点

#### 4.1.1 概念说明

「Anchor（锚点）」是 GE 表达连边的基本抽象。可以把它想象成节点上的一个**接线端子**：

- 端子属于某个节点（`owner_node`）；
- 端子在节点上有一个位置编号（`idx`）；
- 端子内部保存着一个列表，记录「和我连着的对端端子们」（`peer_anchors_`）。

两个端子互相把对方记进自己的列表，一条「边」就建立了。**这里没有一个叫 `Edge` 的对象**——边就是「两个锚点互相持有对方的引用」这一关系本身。

GE 为什么要这样设计？如果用独立的 `Edge` 对象，遍历邻居就要经历 `Edge → Anchor → Node` 的两次跳转，删边时要同步更新两端的引用，边的生命周期管理也更复杂。锚点方案把这些麻烦都消掉了（详见 4.1.2 的收益分析）。

#### 4.1.2 核心流程

锚点类的继承体系如下（出自 [ascend-ir.md 设计文档](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/modules/graph_metadef/ascend-ir.md)）：

```
Anchor（抽象基类）
├── DataAnchor
│   ├── InDataAnchor     输入数据锚点（每个端口一个，只能接 1 个上游）
│   └── OutDataAnchor    输出数据锚点（可接多个下游，支持扇出）
└── ControlAnchor
    ├── InControlAnchor  输入控制锚点（每个节点固定 1 个）
    └── OutControlAnchor 输出控制锚点（每个节点固定 1 个）
```

锚点是在**节点创建**时一并生成的，流程是：

1. `Node::Init` 读取 OpDesc 的输入数量 `N`，创建 `N` 个 `InDataAnchor`，编号 `0..N-1`。
2. 读取 OpDesc 的输出数量 `M`，创建 `M` 个 `OutDataAnchor`，编号 `0..M-1`。
3. **无论 OpDesc 长什么样，都固定再创建一对 `InControlAnchor` 和 `OutControlAnchor`**，编号为 `-1`（用一个非法的数据端口编号来区分「这是控制端子」）。
4. 节点持有这些锚点；后续要连边时，调用锚点的 `LinkTo`/`LinkFrom`。

锚点方案的核心收益（设计文档总结）：

- **O(1) 邻居访问**：从输入锚点直接拿到唯一的对端输出锚点，从输出锚点直接遍历所有对端输入锚点，不需要全局查找。
- **原子性连接/断开**：`LinkTo`/`Unlink` 同时修改两端，保证一致性。
- **图变换友好**：GE 编译器有大量 Pass 需要频繁「断旧边、建新边」，锚点让这些操作非常局部化。
- **避免循环引用**：对端引用用 `weak_ptr`，不会造成「节点互相持有导致无法释放」的内存泄漏。

#### 4.1.3 源码精读

先看基类 `Anchor` 的声明。它继承自 `enable_shared_from_this`（因为要在 `LinkTo` 时把「自己」以 `shared_ptr` 形式塞进对端的列表），并持有一个 `AnchorImplPtr impl_`：

[inc/graph_metadef/graph/anchor.h:L32-L49](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/anchor.h#L32-L49) —— 这是 `Anchor` 抽象基类，定义了 `GetPeerAnchors`、`Unlink`、`Insert`、`ReplacePeer` 等所有锚点通用的连接操作接口。

真正的「对端列表」藏在实现类里。打开 `anchor.cc`，可以看到 `AnchorImpl` 的核心成员：

[graph_metadef/graph/normal_graph/anchor.cc:L53-L61](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L53-L61) —— `peer_anchors_` 是一个 `vector<weak_ptr<Anchor>>`，这就是「双向邻接表」的存根：每个锚点都记着它的对端。用 `weak_ptr` 而不是 `shared_ptr`，正是为了避免节点之间互相持有强引用造成内存泄漏。

`owner_node_` 也是 `weak_ptr`，`owner_node_ptr_` 是一个缓存的裸指针方便快速取所属节点。

再看锚点是怎么在节点初始化时被批量创建的：

[graph_metadef/graph/normal_graph/node.cc:L61-L84](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/node.cc#L61-L84) —— `Node::NodeImpl::Init` 按 OpDesc 的输入/输出数量循环创建 `InDataAnchor`/`OutDataAnchor`（编号 `i`），最后**固定**创建一对控制锚点，编号传 `-1`。

> 对照 4.1.2 的流程图：这段代码就是「锚点从哪来」的答案——锚点不是用户手动 new 的，而是节点 `Init` 时根据 OpDesc 自动生成的。这也解释了为什么上一讲说「Node 持有 OpDesc 与 Anchor」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「每个节点都恰好拥有一对控制锚点，且控制锚点的 idx 是 -1」。

**操作步骤**：

1. 打开 [graph_metadef/graph/normal_graph/node.cc:L61-L84](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/node.cc#L61-L84)。
2. 数一下：循环创建了哪两类数据锚点？它们用的是 `i`（循环变量）作为 idx 吗？
3. 找到控制锚点创建那两行（`InControlAnchor`、`OutControlAnchor`），确认传入的 idx 是 `-1`。
4. 思考：为什么控制锚点不像数据锚点那样「按端口数量循环创建」？

**需要观察的现象**：数据锚点的数量随 OpDesc 输入/输出数量变化（比如一个 2 输入 1 输出的算子会有 2 个 `InDataAnchor` + 1 个 `OutDataAnchor`），但控制锚点**永远是各一个**、与算子形状无关。

**预期结果**：你能用一句话总结——「数据锚点的数量 = 算子的输入/输出端口数；控制锚点恒为 1 对，idx=-1，与端口数无关」。

**运行说明**：本实践为源码阅读型，不需要编译运行；如需运行验证，可参考本讲 4.3.4 的单测实践。

#### 4.1.5 小练习与答案

**练习 1**：`AnchorImpl::peer_anchors_` 为什么用 `vector<weak_ptr<Anchor>>` 而不是 `vector<shared_ptr<Anchor>>`？

**参考答案**：如果两个节点 A、B 的锚点用 `shared_ptr` 互相持有对方，就形成了循环引用（A 的锚点持有 B 的锚点、B 的锚点又持有 A 的锚点），导致引用计数永远降不到 0，节点无法被析构，造成内存泄漏。用 `weak_ptr` 不增加引用计数，打破了环；真正所属关系由「节点持有锚点」这一单向 `shared_ptr` 维系。

**练习 2**：控制锚点的 `idx` 为什么设成 `-1`？

**参考答案**：数据锚点的 `idx` 是有意义的端口编号（从 0 开始，对应 OpDesc 的第几个输入/输出）。控制锚点不对应任何具体数据端口，所以用一个「明显非法」的值 `-1` 来标识「我是控制端子，不是数据端口」。这在序列化时也会被用到（控制边序列化成 `"node_name:-1"`，详见 u2-l1 提到的序列化设计）。

---

### 4.2 数据边与控制边：DataAnchor 与 ControlAnchor

#### 4.2.1 概念说明

`Anchor` 基类有两个直接子类，分别对应两类边：

- **`DataAnchor`**：表达**数据边**。它的子类 `InDataAnchor`（输入数据锚点）和 `OutDataAnchor`（输出数据锚点）承载真正流动的张量。一条数据边 `OutDataAnchor -- LinkTo --> InDataAnchor` 意味着「上游算子的第 i 个输出张量，喂给下游算子的第 j 个输入」。
- **`ControlAnchor`**：表达**控制边**。它的子类 `InControlAnchor`/`OutControlAnchor` 只表达「先后顺序」依赖，不承载张量。

两者最关键的区别在于**连接约束**：

| 锚点 | 允许的对端数量 | 含义 |
|------|--------------|------|
| `InDataAnchor` | **至多 1 个** | 一个输入端口只能由一个上游供给 |
| `OutDataAnchor` | **任意多个** | 一个输出可以扇出给多个下游 |
| `InControlAnchor` | 任意多个 | 可接受多个控制依赖来源 |
| `OutControlAnchor` | 任意多个 | 可向多个节点施加控制依赖 |

此外，GE 还支持**跨类型连接**：`OutDataAnchor` 可以连到 `InControlAnchor`（「我产出的数据还顺便要求你在我之后执行」），`OutControlAnchor` 也可以连到 `InDataAnchor`。

#### 4.2.2 核心流程

建立一条数据边的流程（以 `OutDataAnchor::LinkTo(InDataAnchor)` 为例）：

```
1. 检查 dest 非空、dest 的对端列表为空（保证 InDataAnchor 只接 1 个上游）
2. this->peer_anchors_.push_back(dest)        // 我记下你
3. dest->peer_anchors_.push_back(this)        // 你也记下我
4. 记录一条 trace（用于图变换溯源）
```

注意第 2、3 步是**双向**的——这是「双向邻接表」的核心。断开（`Unlink`）时也必须**两端同时删除**，否则会出现「我记得你、你不记得我」的不一致。

由于 `InDataAnchor` 只允许一个对端，所以有两条等价的建边路径：
- 输入端主动拉：`in_anchor->LinkFrom(out_anchor)`；
- 输出端主动推：`out_anchor->LinkTo(in_anchor)`。

两者内部都做同样的「互 push」，并都先校验「输入端当前对端为空」。

#### 4.2.3 源码精读

先看 `OutDataAnchor::LinkTo(InDataAnchor)` 的实现——这是最典型的一条数据边建立过程：

[graph_metadef/graph/normal_graph/anchor.cc:L499-L517](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L499-L517) —— 第 500 行的判断 `!dest->impl_->peer_anchors_.empty()` 就是「InDataAnchor 只能接一个上游」的硬约束；第 510、511 行两端互相 `push_back`，完成双向连接。

再看输入端视角的 `InDataAnchor::LinkFrom`，约束出现在第 396 行：

[graph_metadef/graph/normal_graph/anchor.cc:L394-L412](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L394-L412) —— 注释 `// InDataAnchor must be only linkfrom once` 明确写了「只能 LinkFrom 一次」，条件里 `!impl_->peer_anchors_.empty()` 一旦为真（已经有对端）就直接返回失败。这个约束还被抽成了一个公共检查函数 `CanAddPeer`：

[graph_metadef/graph/normal_graph/anchor.cc:L22-L29](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L22-L29) —— `CanAddPeer` 统一表达「如果目标是 `InDataAnchor` 且已有对端，则拒绝」，供 `Insert`/`ReplacePeer` 等图变换操作复用。

跨类型连接的证据——`OutDataAnchor` 除了能连 `InDataAnchor`，还重载了一个连 `InControlAnchor` 的 `LinkTo`：

[inc/graph_metadef/graph/anchor.h:L197-L201](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/anchor.h#L197-L201) —— `OutDataAnchor` 提供两个 `LinkTo` 重载：一个连数据输入端，一个连控制输入端（数据→控制依赖）。其实现见 [anchor.cc:L519-L536](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L519-L536)，注意它**没有**「对端必须为空」的检查——因为控制端子允许多对端。

控制锚点的构造也印证了 4.1 里说的「idx = -1」：

[graph_metadef/graph/normal_graph/anchor.cc:L587-L589](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L587-L589) —— `ControlAnchor(owner_node)` 单参数构造直接把 idx 写死为 `-1`，与数据锚点（idx 由端口编号决定）区分开。

#### 4.2.4 代码实践

**实践目标**：通过阅读单元测试，确认「同一个 `OutDataAnchor` 可以扇出给多个下游，但同一个 `InDataAnchor` 不能接第二个上游」。

**操作步骤**：

1. 打开锚点单测 [tests/graph_metadef/ut/graph/testcase/anchor_unittest.cc:L204-L213](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/tests/graph_metadef/ut/graph/testcase/anchor_unittest.cc#L204-L213)。
2. 阅读这段测试：一个 `out_anch` 先后 `LinkTo` 了一个 `InDataAnchor` 和一个 `InControlAnchor`。
3. 观察断言：`out_anch->GetPeerAnchorsSize()` 期望值是 `2`（说明一个输出端子连了两个对端，扇出成功）。
4. 再回到 [anchor.cc:L394-L412](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L394-L412)，回答：如果对同一个 `InDataAnchor` 调用第二次 `LinkFrom`，会发生什么？

**需要观察的现象**：输出锚点的对端数可以大于 1（多扇出合法）；但对输入数据锚点重复建连会被拒绝。

**预期结果**：`OutDataAnchor` 支持一对多；对已有对端的 `InDataAnchor` 再次 `LinkFrom` 会返回 `GRAPH_FAILED`（见第 396 行条件）。结论：**数据边是「多对一的扇入被禁止、一对多的扇出被允许」**。

**运行说明**：若要在本地实际跑这个单测验证，可使用 `ge-dt-runner` skill 编译运行 `anchor_unittest` 目标；若暂无编译环境，本实践以阅读断言、推理行为为主，属源码阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：假设算子 A 的输出 0 同时喂给 B 的输入 0 和 C 的输入 0，这在 GE 里需要建立几条「边」、涉及哪些锚点？

**参考答案**：只有 A 上的 1 个 `OutDataAnchor(idx=0)`，但它有 2 个对端：B 的 `InDataAnchor(idx=0)` 和 C 的 `InDataAnchor(idx=0)`。建两次边：`A.out[0]->LinkTo(B.in[0])` 和 `A.out[0]->LinkTo(C.in[0])`。由于没有独立 Edge 对象，所谓「两条边」其实是 A 的那个输出锚点的 `peer_anchors_` 列表里有 2 个元素。

**练习 2**：为什么 `OutDataAnchor::LinkTo(InControlAnchor)` 不需要检查「对端为空」，而 `LinkTo(InDataAnchor)` 需要？

**参考答案**：`InControlAnchor` 设计上允许有任意多个对端（一个节点可以依赖多个前置节点的控制信号），所以建连时不需要保证对端为空。而 `InDataAnchor` 只允许 1 个上游供给（否则数据来源歧义），所以必须先检查它的 `peer_anchors_` 为空才允许建连。

---

### 4.3 对端锚点遍历：从 OutDataAnchor 找到所有下游消费节点

#### 4.3.1 概念说明

知道了边是「锚点互持对端」之后，最常用的操作就是**遍历对端**——也就是顺着边找到上下游节点。这一节回答本讲的核心实践问题：

> 给定一个 `OutDataAnchor`，如何得到它所有下游消费节点？

由于 `OutDataAnchor` 的 `peer_anchors_` 里存的就是各个对端输入锚点，而每个输入锚点又能 `GetOwnerNode()` 得到它所属的节点，所以答案是一条两跳的路径：

```
OutDataAnchor  --(它的 peer 列表)-->  各个 InDataAnchor  --(所属节点)-->  下游 Node
```

GE 还提供了一套「节点级」的便捷接口（如 `Node::GetOutDataNodesPtr`），把这两跳封装成一步。此外，项目有一条重要的**性能约定**（见仓库 `AGENTS.md` 代码风格一节）：**只读遍历优先用返回裸指针的版本**，因为它不需要构造 `shared_ptr`，性能更好。

#### 4.3.2 核心流程

「拿到一个 `OutDataAnchor` 的所有下游消费节点」的伪代码：

```text
函数 GetAllDownstreamNodes(out_anchor):
    downstreams = 空列表
    # 第 1 跳：输出锚点 -> 它所有对端的输入数据锚点
    for in_anchor in out_anchor.GetPeerInDataAnchorsPtr():   # 裸指针版本，性能更好
        # 第 2 跳：输入锚点 -> 它所属的节点
        node = in_anchor.GetOwnerNodeBarePtr()
        if node != nullptr:
            downstreams.append(node)
    return downstreams
```

对应到源码，两跳分别由：

- 第 1 跳：`OutDataAnchor::GetPeerInDataAnchorsPtr()`（遍历 `peer_anchors_`，过滤出 `InDataAnchor` 类型）；
- 第 2 跳：`Anchor::GetOwnerNodeBarePtr()`（从锚点取所属节点的裸指针）。

#### 4.3.3 源码精读

第 1 跳的实现——`OutDataAnchor::GetPeerInDataAnchorsPtr`：

[graph_metadef/graph/normal_graph/anchor.cc:L458-L470](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L458-L470) —— 遍历 `peer_anchors_`，用 `DynamicAnchorPtrCast<InDataAnchor>` 把每个对端**安全地向下转型**为 `InDataAnchor *`，转型失败（即对端不是数据输入锚点，比如是控制锚点）就跳过。这就是「过滤出数据边对端」的逻辑。

> 注意 `DynamicAnchorPtrCast` 的安全性：它先用 `IsTypeIdOf<T>()` 判断类型是否匹配，不匹配就返回 `nullptr`（见 [anchor.h:L110-L117](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/anchor.h#L110-L117)）。这是 GE 在「一个 `peer_anchors_` 列表里可能混有数据锚点和控制锚点」时做类型分发的标准手段。

与之对照，输入端只有一个对端，所以取对端是「取唯一一个」：

[graph_metadef/graph/normal_graph/anchor.cc:L386-L392](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L386-L392) —— `InDataAnchor::GetPeerOutAnchor` 直接取 `peer_anchors_.begin()` 的第一个（也是唯一一个）元素并转型为 `OutDataAnchor`。

第 2 跳——`GetOwnerNodeBarePtr` 由基类提供，见 [anchor.h:L69](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/anchor.h#L69)。

**节点级便捷接口**：如果不想手动两跳，`Node` 直接提供了「输出数据节点」列表，一步到位：

[inc/graph_metadef/graph/node.h:L168-L169](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L168-L169) —— `GetOutDataNodes()` 返回 `shared_ptr` 版本（适合边遍历边修改图），`GetOutDataNodesPtr()` 返回裸指针版本（只读场景，性能更好）。同理还有 `GetInNodesPtr`、`GetOutNodesPtr` 等（[node.h:L137](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L137)、[node.h:L149](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L149)）。

**关于性能约定**：`AGENTS.md` 的代码风格一节明确要求「使用 `GetPeerInDataAnchorsPtr` 代替 `GetPeerInDataAnchors`，前者不需要构造智能指针，性能更好」。两类接口的对照见 [anchor.h:L190-L191](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/anchor.h#L190-L191)：

- `GetPeerInDataAnchors()` 返回 `Vistor<InDataAnchorPtr>`（每个元素是 `shared_ptr`，有引用计数开销）；
- `GetPeerInDataAnchorsPtr()` 返回 `std::vector<InDataAnchor *>`（裸指针，无开销）。

**实际加边入口**：写 Pass 时一般不会直接调 `LinkTo`，而是用 `GraphUtils::AddEdge`，它只是 `LinkTo` 的薄封装：

[graph_metadef/graph/utils/graph_utils.cc:L187-L195](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/utils/graph_utils.cc#L187-L195) —— `GraphUtils::AddEdge(OutDataAnchorPtr, InDataAnchorPtr)` 内部就是调一句 `src->LinkTo(dst)`，外加空指针与失败检查。`AddEdge` 还有针对控制边、跨类型边的多个重载（见 [graph_utils.h:L132-L138](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/utils/graph_utils.h#L132-L138)）。

#### 4.3.4 代码实践

**实践目标**：完成本讲规定的实践——编写伪代码，给定一个 `OutDataAnchor` 得到它所有下游消费节点。

**操作步骤**：

1. 在 [inc/graph_metadef/graph/anchor.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/anchor.h) 中找到 `DataAnchor`（L133）与 `ControlAnchor`（L210）的定义，确认它们的子类。
2. 定位 `OutDataAnchor::GetPeerInDataAnchorsPtr`（[anchor.h:L191](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/anchor.h#L191)）与 `Anchor::GetOwnerNodeBarePtr`（[anchor.h:L69](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/anchor.h#L69)）。
3. 写出下面这段「最小调用示例」（**示例代码**，非项目原有代码，仅供理解）：

```cpp
// 示例代码：给定 OutDataAnchor *out，收集它所有下游消费 Node（裸指针，只读场景）
std::vector<Node *> GetDownstreamConsumers(OutDataAnchor *out) {
  std::vector<Node *> consumers;
  if (out == nullptr) { return consumers; }
  // 第 1 跳：输出锚点 -> 所有对端输入数据锚点（裸指针版本，符合 AGENTS.md 性能约定）
  for (InDataAnchor *in : out->GetPeerInDataAnchorsPtr()) {
    // 第 2 跳：输入锚点 -> 所属节点
    Node *n = in->GetOwnerNodeBarePtr();
    if (n != nullptr) { consumers.push_back(n); }
  }
  return consumers;
}

// 等价的「一步到位」写法（如果手上是 Node，直接用节点级接口）
std::vector<Node *> GetDownstreamByNode(Node *node) {
  return node->GetOutDataNodesPtr();  // 已封装好两跳
}
```

4. 对照 [anchor.cc:L458-L470](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L458-L470) 验证：`GetPeerInDataAnchorsPtr` 内部确实是用 `DynamicAnchorPtrCast<InDataAnchor>` 过滤类型的，因此跨类型连接里的控制锚点对端不会被误当成数据下游。

**需要观察的现象**：两跳路径 `OutDataAnchor → InDataAnchor → Node` 的每一步都能在源码里找到对应函数；裸指针版本与 `shared_ptr` 版本在头文件里成对出现。

**预期结果**：你能独立写出上述伪代码，并能解释「为什么用 `Ptr` 后缀的裸指针版本」（性能约定），以及「为什么遍历时不会把控制对端也算进来」（`DynamicAnchorPtrCast` 类型过滤）。

**运行说明**：本实践以源码阅读 + 伪代码编写为主，无需运行。若想真正跑通，可把示例函数写进一个 UT（参考 [anchor_unittest.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/tests/graph_metadef/ut/graph/testcase/anchor_unittest.cc) 的 `builder.AddNode` 用法构造图），用 `ge-dt-runner` skill 编译运行。

#### 4.3.5 小练习与答案

**练习 1**：`GetPeerInDataAnchors()`（返回 `Vistor<InDataAnchorPtr>`）和 `GetPeerInDataAnchorsPtr()`（返回 `vector<InDataAnchor *>`）有什么区别？写 Pass 时该优先用哪个？

**参考答案**：前者返回的是 `shared_ptr` 集合，每次构造都有引用计数加减开销，但适合「边遍历边修改图」（智能指针保证遍历过程中对象不被释放）；后者返回裸指针集合，无开销，适合**只读**遍历。按 `AGENTS.md` 的约定，**只读场景优先用裸指针版本**以获得更好性能。

**练习 2**：如果只给你一个 `InDataAnchor`，怎样找到它的上游生产节点？需要遍历列表吗？

**参考答案**：不需要遍历列表。因为 `InDataAnchor` 至多只有一个对端，直接调用 `GetPeerOutAnchor()`（[anchor.cc:L386-L392](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L386-L392)）拿到唯一的上游 `OutDataAnchor`，再 `GetOwnerNodeBarePtr()` 即可。这正是锚点方案「O(1) 邻居访问」收益的体现。

**练习 3**：`GraphUtils::AddEdge(A.out, B.in)` 和直接调 `A.out->LinkTo(B.in)` 效果一样吗？为什么 GE 还要提供 `AddEdge`？

**参考答案**：效果一样——`AddEdge` 内部就是调 `LinkTo`（见 [graph_utils.cc:L187-L195](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/utils/graph_utils.cc#L187-L195)）。提供 `AddEdge` 是为了：① 统一加边入口，提供空指针与失败检查；② 用重载屏蔽「数据边/控制边/跨类型边」的细节，调用方传什么类型的锚点就自动匹配对应的 `LinkTo`；③ 与 `RemoveEdge`/`ReplaceEdgeSrc`/`ReplaceEdgeDst` 等成套图操作工具放在一起，风格一致。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「图结构小实验」（源码阅读 + 推理型）：

**任务**：设想一张极简图 `Data → Add → Relu`（`Data` 是数据输入节点，`Add` 有两个输入但这里只用一个，`Relu` 接 `Add` 的输出）。请回答并验证：

1. **数锚点**：`Add` 节点 `Init` 后，分别拥有几个 `InDataAnchor`、几个 `OutDataAnchor`、几个 `InControlAnchor`/`OutControlAnchor`？（提示：看 [node.cc:L61-L84](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/node.cc#L61-L84)，控制锚点与端口数无关。）
2. **建边**：要把 `Add` 的输出连到 `Relu` 的输入，用 `GraphUtils::AddEdge` 写出调用（提示：需要先从节点取出锚点 `add->GetOutDataAnchor(0)`、`relu->GetInDataAnchor(0)`，参见 [node.h:L124-L125](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L124-L125)）。
3. **遍历**：建边后，用 4.3 的两跳方法，从 `Add` 的 `OutDataAnchor(0)` 找到它的下游消费节点，验证结果应是 `Relu`。
4. **删边**：若再调用一次 `add.out[0]->LinkTo(relu.in[0])`（重复建同一条边），会成功吗？为什么？（提示：结合 `InDataAnchor` 的单上游约束思考。）

**参考结论**：

1. `Add`（假设 OpDesc 声明 2 输入 1 输出）：2 个 `InDataAnchor`、1 个 `OutDataAnchor`、各 1 个控制锚点（idx=-1）。
2. `GraphUtils::AddEdge(add->GetOutDataAnchor(0), relu->GetInDataAnchor(0));`
3. `add->GetOutDataAnchor(0)->GetPeerInDataAnchorsPtr()` 取到 `Relu` 的输入锚点，再 `GetOwnerNodeBarePtr()` 得到 `Relu` 节点。
4. 不会成功——第二次建边时，`Relu.in[0]` 的 `peer_anchors_` 已非空，`LinkTo`/`LinkFrom` 的检查（[anchor.cc:L500](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/anchor.cc#L500)）会返回 `GRAPH_FAILED`。

> **待本地验证**：第 1、4 问涉及具体行为，建议在本地用 `ge-dt-runner` 跑一个临时 UT 实测确认；其余为源码可直接读出的结论。

---

## 6. 本讲小结

- GE **不设独立 Edge 对象**，连边由节点内嵌的**锚点（Anchor）**互相持有对端引用来表达，对端列表用 `weak_ptr` 避免循环引用。
- 锚点体系是 `Anchor` 基类下两支四类：`DataAnchor`（`InDataAnchor`/`OutDataAnchor`）表达数据边，`ControlAnchor`（`InControlAnchor`/`OutControlAnchor`）表达控制边；每个节点固定拥有一对控制锚点（idx=-1）。
- 数据边的关键约束：**`InDataAnchor` 至多 1 个上游**（`LinkFrom`/`LinkTo` 会校验对端为空），**`OutDataAnchor` 可多扇出**；控制边无此限制，且支持跨类型连接。
- `LinkTo`/`LinkFrom` 是**双向**操作：两端 `peer_anchors_` 互 push，`Unlink` 则两端互删，保证一致性。
- 遍历下游：`OutDataAnchor → GetPeerInDataAnchorsPtr → GetOwnerNodeBarePtr` 两跳拿到消费节点；只读场景优先用裸指针 `Ptr` 版本（项目性能约定）。
- 实际写 Pass 加边/删边一般用 `GraphUtils::AddEdge`/`RemoveEdge`，它们是对 `LinkTo`/`Unlink` 的薄封装。

---

## 7. 下一步学习建议

本讲把「连边」机制讲透了。至此你已经掌握了 AscendIR 的**拓扑结构**——节点 + 锚点连边。但一个节点「是什么算子、输入输出张量长什么样、有哪些属性」还没展开，这正是下一讲 **u2-l3 OpDesc 算子描述：输入输出与属性** 的内容。建议：

1. 先读 [inc/graph_metadef/graph/op_desc.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h)，看看 `OpDesc` 如何描述一个算子的输入输出 `GeTensorDesc` 与属性，理解它与本讲的 `Node`/`Anchor` 如何组合。
2. 再带着本讲的视角去读：`Node::Init` 创建数据锚点时，数量正是取自 `OpDesc` 的输入/输出数量（[node.cc:L61-L82](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/node.cc#L61-L82)）——锚点和 OpDesc 在这里咬合。
3. 后续 **u2-l4 算子注册与原型体系** 会解释这些 `OpDesc` 里的算子类型、输入输出定义是从哪里「注册」进来的，把 AscendIR 单元收尾。
