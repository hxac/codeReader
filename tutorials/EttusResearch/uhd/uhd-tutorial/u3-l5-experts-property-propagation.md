# 属性传播与 experts 框架

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「为什么 RF 配置不能靠每个 setter 里手写一串副作用」这个动机问题，并理解 UHD 用**依赖图（DAG）**来组织派生计算的设计思路。
- 读懂 experts 框架的三类顶点（数据节点 / worker 节点 / property 节点）、reader/writer 访问器，以及驱动整个框架的**脏标记（dirty flag）**机制。
- 用 `expert_factory` 把数据节点、worker 节点「粘」进容器，并理解它如何与 `property_tree`（u2-l4）的 desired/coerced 双值模型对接。
- 读懂 `expert_container` 如何用**拓扑排序**驱动一次完整求解，理解两遍扫描（先 resolve、后 mark_clean）与自动解析（auto-resolve）回调。
- 区分清楚：本讲的 experts 框架（运行在子板 dboard 层）与 RFNoC 流图 `commit()` 里的 `resolve_all_properties()`（运行在块层）是**两套不同机制**，不要混为一谈。

---

## 2. 前置知识

本讲建立在两讲之上，请先确认你已经掌握：

- **u2-l4 属性树 property_tree**：尤其是「双值模型」——一个属性同时持有 `desired`（期望值）与 `coerced`（强制值，`coerced = coerce(desired)`），以及 coercer / subscriber / publisher 三类回调。本讲的 experts 框架正是 property_tree 双值模型的「计算引擎」。
- **u3-l2 RFNoC 块控制器**：知道 RFNoC 块有自己的一套属性系统。本讲最后会专门说明它与 experts 框架的边界。

另外补充两个通用概念：

### 2.1 脏标记（dirty flag）

一个值被修改后就被标记为「脏（dirty）」，表示「我的下游需要重新计算」。下游消费完这个值后，再把它「清理（clean）」。这是增量计算的经典手法：只有真正变化过的值才需要传播，未变化的值跳过。UHD 把这个模式做成了一个可复用的小类 `dirty_tracked<T>`（见 4.1.3）。

### 2.2 有向无环图（DAG）与拓扑排序

experts 把所有计算关系建模成一张**有向图**：数据节点和 worker 节点是顶点，「A 是 B 的输入」是边。这张图必须是**无环的**——否则就会出现「A 依赖 B、B 又依赖 A」的死循环。

对 DAG 求**拓扑序（topological order）**，就是要找一个线性排列，使得对任意有向边 \(u \to v\)，\(u\) 都排在 \(v\) 前面：

\[
\forall (u,v) \in E,\quad \mathrm{pos}(u) < \mathrm{pos}(v)
\]

按拓扑序逐个 resolve，就能保证「一个 worker 被调用时，它所有输入都已经是最新值」。本讲的求解器正是基于 Boost Graph Library 的 `topological_sort`。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [host/include/uhd/experts/expert_nodes.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp) | **图的顶点定义**：`dag_vertex_t` 基类、`data_node_t` 数据节点、`worker_node_t` worker 节点、reader/writer 访问器。 |
| [host/include/uhd/experts/expert_factory.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_factory.hpp) | **建图工厂**：一套模板函数，把节点造好并塞进容器，并把数据节点与 `property_tree` 桥接起来。 |
| [host/include/uhd/experts/expert_container.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_container.hpp) | **容器抽象接口**：`resolve_all` / `resolve_from` / `resolve_to` / `to_dot` 等求解入口。 |
| [host/lib/experts/expert_container.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp) | **求解器实现**：用 BGL `adjacency_list` 存图，`topological_sort` 驱动两遍扫描求解。 |
| [host/include/uhd/utils/dirty_tracked.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/dirty_tracked.hpp) | 脏标记原语，`data_node_t` 内部值的载体。 |
| [host/tests/expert_test.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/expert_test.cpp) | **官方教学用例**：一张小巧完整的 DAG，是理解整个框架的最佳入口。 |
| [host/lib/usrp/dboard/zbx/zbx_expert.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/zbx/zbx_expert.cpp) | **真实工程用例**：ZBX 子板把「调谐频率 → LO 频率/滤波器/频带反转」这条复杂链路写成了一串 worker。 |

> 约定：本讲的「数据节点」指 `data_node_t`（图里的值），「worker 节点」指 `worker_node_t`（图里的计算），「property 节点」指带回调、可被 `property_tree` 外部访问的那类数据节点。三者关系见 4.1。

---

## 4. 核心概念与源码讲解

### 4.1 expert_nodes：DAG 的顶点、访问器与脏标记

#### 4.1.1 概念说明

先想一个具体问题：用户对一块 ZBX 子板调用 `set_rx_frequency(2.4e9)`。这一个动作背后，硬件真正需要的远不止一个数字——需要算出 LO1/LO2 的频率、选择 RF/IF1/IF2 滤波器、判断当前频带是否需要 IQ 频带反转、决定 NCO 频率……而这些派生量之间还有依赖（LO2 频率依赖 IF2，IF2 又依赖调谐频率）。

如果把这些计算都塞进 `set_rx_frequency` 的函数体里，会得到一个几百行的「上帝函数」：顺序耦合、无法测试、改一处牵动全身。experts 框架给出的解法是**声明式依赖图**：

- 你只声明「有哪些值」（**数据节点**）和「有哪些计算规则」（**worker 节点**）。
- 你通过 reader/writer 访问器声明依赖关系（谁读谁、谁写谁）。
- 框架自动推导出拓扑序，在某个上游值变化后，按正确顺序把所有受影响的 worker 跑一遍。

这样，调谐频率的变化会**自动传播**到 LO、滤波器、频带反转等一系列下游，无需你手写调用顺序。

#### 4.1.2 核心流程：脏标记如何沿边传播

整张图的运转围绕「脏」展开，规则非常简单：

1. **数据节点的脏**：值被改写（且新值 ≠ 旧值）即变脏；`mark_clean()` 清理。
2. **worker 节点的脏**：只要它的**任一输入**是脏的，它就是脏的（见 `worker_node_t::is_dirty`，4.1.3）。
3. **求解**：按拓扑序遍历，对每个脏的节点调用 `resolve()`；worker 的 `resolve()` 会读取输入、写出输出，写输出会把输出数据节点标记为脏，从而把「脏」继续往后传。
4. **清理**：一次求解结束后，把所有跑过的 worker 的输入标记为 clean——这样下一轮只有真正再变化过的值才会重新触发计算。

用伪代码描述一次 `resolve_all`：

```
nodes = topological_sort(graph)          # 保证 u→v 时 u 在前
resolved = []
for n in nodes:                          # 第一遍：按序 resolve 脏节点
    if force or n.is_dirty():
        n.resolve()
        if n is worker: resolved.append(n)
for w in resolved:                       # 第二遍：清理 worker 的输入
    w.mark_clean()
```

注意第二遍只清理「被 worker 消费过的输入」——那些没有任何 worker 读取的数据节点会保持脏，这是合理的（没人消费，自然不需要清理）。

#### 4.1.3 源码精读

**三类节点身份**用三个枚举区分（[host/include/uhd/experts/expert_nodes.hpp:28-30](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L28-L30)）：

- `node_class_t`：`CLASS_WORKER`（计算）/ `CLASS_DATA`（纯内部数据）/ `CLASS_PROPERTY`（可被外部 property_tree 访问的数据）。
- `node_access_t`：`ACCESS_READER` / `ACCESS_WRITER`，给访问器用。
- `node_author_t`：`AUTHOR_NONE/USER/EXPERT`，记录这个值最后是被谁写的（用户写还是专家计算写），便于排查。

**`dag_vertex_t` 是所有顶点的抽象基类**（[host/include/uhd/experts/expert_nodes.hpp:39-81](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L39-L81)），定义了图求解所需的纯虚接口 `is_dirty / mark_clean / force_dirty / resolve`，以及读写回调接口。注意构造函数是 `protected`——顶点不能随意 new，必须由框架工厂创建。

**`data_node_t<T>` 是带类型的值节点**（[host/include/uhd/experts/expert_nodes.hpp:135-275](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L135-L275)）。它的关键设计有两点：

第一，**是否传回调互斥锁决定它是 DATA 还是 PROPERTY**（[host/include/uhd/experts/expert_nodes.hpp:144-150](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L144-L150)）：

```cpp
data_node_t(const std::string& name, std::recursive_mutex* mutex = NULL)
    : dag_vertex_t(mutex ? CLASS_PROPERTY : CLASS_DATA, name)
    , _callback_mutex(mutex), ...
```

传了 `mutex`（即容器的 `resolve_mutex`）→ 这个节点会被 property_tree 从外部读写，标记为 `CLASS_PROPERTY`；没传 → 纯内部 `CLASS_DATA`。

第二，**脏逻辑全部委托给 `dirty_tracked<T>`**（[host/include/uhd/experts/expert_nodes.hpp:179-192](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L179-L192)）。`dirty_tracked` 的赋值运算符在「新值 ≠ 旧值」时才置脏（[host/include/uhd/utils/dirty_tracked.hpp:87-94](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/dirty_tracked.hpp#L87-L94)）：

```cpp
inline dirty_tracked& operator=(const data_t& value) {
    if (!(_data == value)) {   // 要求 data_t 必须有 operator==
        _dirty = true;
        _data  = value;
    }
    return *this;
}
```

这就是「值没变就不传播」的根源——也解释了为什么 `data_t` 必须支持 `==`（见 `data_node_t` 类注释对 `data_t` 的四项要求）。

第三，**两条写入路径**：

- `set()`（[host/include/uhd/experts/expert_nodes.hpp:200-204](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L200-L204)）：框架/worker 内部写入，`_author = AUTHOR_EXPERT`，不触发回调。
- `commit()`（[host/include/uhd/experts/expert_nodes.hpp:212-224](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L212-L224)）：外部（property_tree）写入，加锁、`_author = AUTHOR_USER`，**若变脏且有 write 回调则触发它**。这正是「auto-resolve on write」的入口。

对应的 `retrieve()`（[host/include/uhd/experts/expert_nodes.hpp:226-236](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L226-L236)）是外部读取，触发 read 回调——「auto-resolve on read」的入口。

**`worker_node_t` 是计算节点**（[host/include/uhd/experts/expert_nodes.hpp:481-576](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L481-L576)）。它的脏判断是「任一输入脏即脏」（[host/include/uhd/experts/expert_nodes.hpp:523-530](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L523-L530)），而 `force_dirty` 会把自己的**输出**全部置脏（[host/include/uhd/experts/expert_nodes.hpp:539-544](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L539-L544)）。核心的 `resolve()` 是纯虚函数（[host/include/uhd/experts/expert_nodes.hpp:546](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L546)），由子类实现具体计算。

**依赖关系不是显式声明的，而是靠「访问器」自动建立**。`bind_accessor` 在 worker 构造时被调用，把 reader 放进 `_inputs`、writer 放进 `_outputs`（[host/include/uhd/experts/expert_nodes.hpp:510-519](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L510-L519)）。容器随后读 `_inputs`/`_outputs` 就能推出图的边（见 4.3.3）。访问器基类 `data_accessor_base` 在构造时用 `dynamic_cast` 校验类型，类型不符直接抛 `type_error`（[host/include/uhd/experts/expert_nodes.hpp:352-362](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L352-L362)），保证图里的类型安全。

#### 4.1.4 代码实践：从测试用例拆解 worker 的输入输出

**实践目标**：建立「reader = 输入边、writer = 输出边」的直觉。

**操作步骤**：

1. 打开 [host/tests/expert_test.cpp:18-38](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/expert_test.cpp#L18-L38)，阅读 `worker1_t`：

   ```cpp
   worker1_t(const node_retriever_t& db)
       : worker_node_t("A+B=C"),
         _a(db, "A/desired"), _b(db, "B"), _c(db, "C") {
       bind_accessor(_a); bind_accessor(_b); bind_accessor(_c);
   }
   void resolve() override { _c = _a + _b; }
   data_reader_t<int> _a, _b;   // reader → 输入
   data_writer_t<int> _c;       // writer → 输出
   ```

2. 同样阅读 `worker2_t`（`C*D=E`）、`worker3_t`（`-B=F`）、`worker4_t`（`E-F=G`）、`worker5_t`（消费 `G`）。
3. 把每个 worker 的 reader 写成 `输入 → worker`，writer 写成 `worker → 输出`。

**需要观察的现象**：你会得到一串边，例如 `A/desired→worker1`、`B→worker1`、`worker1→C`、`C→worker2`、`B→worker3`……这就是整张 DAG 的全部边。注意 `worker5_t` 里有一行 `// bind_accessor(_c);` 被注释掉了（[host/tests/expert_test.cpp:119](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/expert_test.cpp#L119)）——它声明了一个 writer `_c` 却没绑定，于是图里**不会**出现 `worker5→C` 这条边。这正是「不 bind 就不建边」的体现。

**预期结果**：手画出的依赖图应如下（`→` 表示数据流方向）：

```
A/desired ─┐
           ├─→ worker1(A+B=C) ─→ C ─┐
B ─────────┤                         ├─→ worker2(C*D=E) ─→ E ─┐
           └─→ worker3(-B=F) ─→ F ───┤                        │
D ────────────────────────────────→ worker2                  │
                                 ┌───────────────────────────┘
                                 └─→ worker4(E-F=G) ─→ G ─→ worker5(Consume_G) ─→ 外部 output
                                 ┌───────────────────┘
                            F ───┘
```

> 待本地验证：若你已编译 host 测试，可运行 `expert_test`；否则纯源码阅读即可完成本实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `data_node_t::resolve()` 是空操作（[host/include/uhd/experts/expert_nodes.hpp:194-197](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_nodes.hpp#L194-L197)）？

**答案**：数据节点是「被动的值」，它没有计算逻辑——它的值要么由外部 `commit` 写入，要么由某个 worker `set` 写入。所以它自身不需要 `resolve` 任何东西；真正干活的是 worker。

**练习 2**：如果一个 worker 同时把同一个数据节点既当 reader 又当 writer 会怎样？

**答案**：这会形成一条「worker → 数据节点 → 同一个 worker」的边，本质是自环/环路。拓扑排序会失败，容器抛 `runtime_error`（4.3.3）。正确做法是「计算」应拆成两个数据节点（输入 desired、输出 coerced）+ 一个 worker，像 `add_dual_prop_node` 那样。

---

### 4.2 expert_factory：建图工厂与 property_tree 桥接

#### 4.2.1 概念说明

`expert_container` 的所有「改图」接口（`add_data_node` / `add_worker` / `clear`）都是 `protected` 的，并且 `expert_container` 把 `expert_factory` 声明为友元（[host/include/uhd/experts/expert_container.hpp:138](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_container.hpp#L138)）。这是一个刻意的封装：**外部代码不能直接改图结构，只能通过 `expert_factory` 的模板函数来建图**。这样所有「造节点、建边、接回调」的样板代码都集中在工厂一处，既安全又一致。

`expert_factory` 最重要的职责是**把 experts 图和 `property_tree` 桥接起来**——这是本讲「认识 experts 与 property_tree 的协作」的核心。回忆 u2-l4：property_tree 的属性有 desired/coerced 双值、有 coercer/subscriber/publisher 回调。experts 框架提供的两种 property 节点恰好对应两种 coercion 策略：

- **`add_prop_node`（单节点）**：property_tree 自己跑 coercer（`AUTO_COERCE`），experts 只负责存这个值。适合「微型矫正」，比如大小写归一化。
- **`add_dual_prop_node`（双节点 desired/coerced）**：property_tree 设为 `MANUAL_COERCE`（不自动矫正），由 experts 图里的 worker 来计算 desired → coerced。适合「正经的派生计算」，比如采样率 → DSP 缩放系数。

#### 4.2.2 核心流程

工厂提供四类操作：

1. `create_container(name)`：造一个空容器（[host/include/uhd/experts/expert_factory.hpp:42](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_factory.hpp#L42)）。
2. `add_data_node<T>(container, name, init_val, mode)`：加一个纯内部数据节点（[host/include/uhd/experts/expert_factory.hpp:58-65](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_factory.hpp#L58-L65)），不接 property_tree。
3. `add_prop_node` / `add_dual_prop_node`：加一个同时挂在 property_tree 和 experts 图里的节点（4.2.3 精读）。
4. `add_worker_node<worker_t>(container, args...)`：造一个 worker 并塞进容器（[host/include/uhd/experts/expert_factory.hpp:244-249](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_factory.hpp#L244-L249)）。worker 在构造时绑定访问器，容器据此自动建边。

桥接的本质是**用 `std::bind` 把数据节点的 `commit`/`retrieve` 绑成 property_tree 的回调**：

- property 的「写订阅」→ 数据节点的 `commit`（外部写入，触发 write 回调 → 可能 auto-resolve）。
- property 的「publisher」→ 数据节点的 `retrieve`（外部读取，触发 read 回调 → 可能 auto-resolve）。

#### 4.2.3 源码精读

**`add_prop_node` 的桥接**（[host/include/uhd/experts/expert_factory.hpp:93-111](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_factory.hpp#L93-L111)）：

```cpp
property<data_t>& prop =
    subtree->create<data_t>(path, property_tree::AUTO_COERCE);          // ① property_tree 自动矫正
data_node_t<data_t>* node_ptr =
    new data_node_t<data_t>(name, init_val, &container->resolve_mutex()); // ② 带锁 → CLASS_PROPERTY
prop.set(init_val);
prop.add_coerced_subscriber(
    std::bind(&data_node_t<data_t>::commit, node_ptr, ...));            // ③ 写 → commit
prop.set_publisher(
    std::bind(&data_node_t<data_t>::retrieve, node_ptr));               // ④ 读 → retrieve
container->add_data_node(node_ptr, mode);
```

注意第②行传入了 `&container->resolve_mutex()`——这个递归互斥锁也是 `resolve_all` 持有的同一把锁（见 4.3.3）。因此**外部经 property_tree 读写数据节点，与容器的求解是互斥串行的**，这就是 experts 与 property_tree 之间线程安全的桥梁。

**`add_dual_prop_node` 的桥接**（[host/include/uhd/experts/expert_factory.hpp:153-184](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_factory.hpp#L153-L184)）：

```cpp
property<data_t>& prop =
    subtree->create<data_t>(path, property_tree::MANUAL_COERCE);        // ① property_tree 不自动矫正
auto desired_node_ptr  = new data_node_t<data_t>(desired_name,  init_val, &mutex);
auto coerced_node_ptr  = new data_node_t<data_t>(coerced_name,  init_val, &mutex);
prop.add_desired_subscriber(
    std::bind(&data_node_t<data_t>::commit, desired_node_ptr, ...));    // ② 写 desired
prop.set_publisher(
    std::bind(&data_node_t<data_t>::retrieve, coerced_node_ptr));       // ③ 读 coerced
```

这里 desired 与 coerced 是**两个独立数据节点**，中间靠你自己注册的 worker 把 desired 算成 coerced（`MANUAL_COERCE` 表示 property_tree 不插手）。于是「用户 set desired → 触发求解 → worker 计算 → coerced 节点更新 → 用户 get 读回 coerced」这条链就完整了。

**auto-resolve 模式的拆分**（[host/include/uhd/experts/expert_factory.hpp:162-165](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_factory.hpp#L162-L165)）：双节点的 `mode` 被拆成两半——desired 节点看是否 `ON_WRITE`、coerced 节点看是否 `ON_READ`（[host/include/uhd/experts/expert_factory.hpp:179-182](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_factory.hpp#L179-L182)）。四种模式（`AUTO_RESOLVE_OFF / ON_READ / ON_WRITE / ON_READ_WRITE`，见 [host/include/uhd/experts/expert_container.hpp:18-23](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/experts/expert_container.hpp#L18-L23)）由此落到两个节点上。

#### 4.2.4 代码实践：追踪一次「写 desired」的连锁反应

**实践目标**：把 4.2.3 的桥接代码与 4.1 的脏机制串起来，看清「写 property」到底触发了什么。

**操作步骤**：

1. 阅读 [host/tests/expert_test.cpp:180-181](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/expert_test.cpp#L180-L181)，注意 `A` 是用 `add_dual_prop_node` 且 `AUTO_RESOLVE_ON_WRITE` 加进去的，于是 desired 节点带 write 回调。
2. 跳到 [host/tests/expert_test.cpp:244-247](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/expert_test.cpp#L244-L247)：

   ```cpp
   tree->access<int>("A").set(200);
   BOOST_CHECK(nodeC.get() == nodeA.get() + nodeB.get());  // 没有显式 resolve_all！
   BOOST_CHECK(nodeE.get() == nodeC.get() * nodeD.get());
   BOOST_CHECK(nodeG.get() == nodeE.get() - nodeF.get());
   ```

3. 对照调用链：`tree->access("A").set(200)` → desired subscriber → `desired_node->commit(200)` → 变脏 + write 回调 → `resolve_from` → 一次完整 `resolve_all` → C/E/G 全部更新。

**需要观察的现象**：在 `set(200)` 之后、**没有**调用 `resolve_all()` 的情况下，断言就成立了。这就是 auto-resolve-on-write 的效果——写入即求解。

**预期结果**：同理，[host/tests/expert_test.cpp:254-258](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/expert_test.cpp#L254-L258) 中 `E` 用 `AUTO_RESOLVE_ON_READ` 加进来，于是 `tree->access<int>("E").get()`（一次读取）也会触发求解，读完后 `!nodeE.is_dirty()` 成立。

#### 4.2.5 小练习与答案

**练习 1**：`add_prop_node` 用 `AUTO_COERCE`，`add_dual_prop_node` 用 `MANUAL_COERCE`，为什么正好相反？

**答案**：单节点场景下，coercion 由 property_tree 自己的 coercer 做就够了（小矫正），experts 只存结果，所以用 `AUTO_COERCE`；双节点场景下，要把 desired 经一串 worker 计算成 coerced，property_tree 不能插手，所以用 `MANUAL_COERCE`，把矫正权完全交给 experts 图。

**练习 2**：为什么桥接时必须传入 `&container->resolve_mutex()`？

**答案**：这把锁让「外部经 property_tree 的 commit/retrieve」与「容器内部的 resolve_all」互斥。否则用户线程正在写 desired、容器线程正在拓扑求解，会出现读到半新半旧值的竞态。同一个 `recursive_mutex` 还允许求解过程中 worker 反过来读 property（递归加锁）而不死锁。

---

### 4.3 expert_container：拓扑排序驱动的求解器

#### 4.3.1 概念说明

容器是图的「持有者 + 求解器」。它做两件事：

1. **存图**：用 Boost Graph Library 的 `adjacency_list` 存顶点和边，另外维护两个名字→顶点的映射（`_datanode_map`、`_worker_map`）方便按名查找。
2. **求解**：对外暴露 `resolve_all / resolve_from / resolve_to` 三个入口，内部统一走 `_resolve_helper`，做拓扑排序 + 两遍扫描。

一个非常重要的实现细节（也是初学者容易踩的坑）：**`resolve_from` 和 `resolve_to` 当前都被覆盖成「做一次完整 `resolve_all`」**（4.3.3 会看到代码与注释）。也就是说，虽然接口签名带 `node_name` 参数，看起来像「只从某个节点开始求解」，但实现里并没有做这种剪枝——文档注释明确写了「为降低 experts 复杂度，不做按 node_name 的遍历优化」。所以 auto-resolve 的几种模式，**区别只在于「什么时候触发一次完整求解」，而不在于「求解了图的哪一部分」**。

#### 4.3.2 核心流程

完整生命周期分三阶段：

**阶段一·建图**（构造期，由 `expert_factory` 驱动）：

```
create_container("name")
  → add_data_node(N):  boost::add_vertex(N); 记进 _datanode_map; 按 mode 挂 write/read 回调
  → add_worker(W):     boost::add_vertex(W); 记进 _worker_map
                       遍历 W.get_inputs()  → 对每个输入 add_edge(输入→W)
                       遍历 W.get_outputs() → 对每个输出 add_edge(W→输出)
```

**阶段二·求解**（`resolve_all`）：

```
topological_sort(graph) → sorted_nodes     # 有环则抛 not_a_dag，转而报告具体回路
for n in sorted_nodes:                     # 第一遍
    if force or n.is_dirty():
        n.resolve()                        # worker 的 resolve 读输入、写输出（写会把下游置脏）
        if n is worker: 记进 resolved
for w in resolved: w.mark_clean()          # 第二遍：清理被消费的输入
```

**阶段三·销毁**（`clear` / 析构）：遍历所有顶点 `delete` 掉节点对象，清空图与两个 map。

#### 4.3.3 源码精读

**图的数据结构**（[host/lib/experts/expert_container.cpp:29-41](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L29-L41)）：`expert_graph_t` 是一个 `boost::adjacency_list`，有向图（`directedS`），每个顶点存一个 `dag_vertex_t*`。两个 `std::map` 分别按名索引数据节点和 worker 节点。

**`add_data_node` 挂回调**（[host/lib/experts/expert_container.cpp:279-328](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L279-L328)）：除了加顶点、查重，关键是按 `resolve_mode` 给数据节点挂 write/read 回调，分别绑到 `resolve_from` / `resolve_to`（[host/lib/experts/expert_container.cpp:311-322](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L311-L322)）。这就把 4.1 讲的 `commit`/`retrieve` 回调接到了「触发求解」上。

**`add_worker` 建边**（[host/lib/experts/expert_container.cpp:330-398](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L330-L398)）：核心两段循环——对每个输入加「输入→worker」边（[host/lib/experts/expert_container.cpp:359-371](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L359-L371)），对每个输出加「worker→输出」边（[host/lib/experts/expert_container.cpp:374-386](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L374-L386)）。若输入/输出的数据节点还没注册，抛 `runtime_error` 并 `clear()` 整图——错误不可恢复。注意建图顺序：**数据节点必须先于引用它的 worker 注册**。

**`resolve_from` / `resolve_to` 被覆盖为完整求解**（[host/lib/experts/expert_container.cpp:88-106](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L88-L106)），注释直说「Not optimizing the traversal using node_name to reduce experts complexity」。这是 4.3.1 提到的关键细节。

**`_resolve_helper` 是求解核心**（[host/lib/experts/expert_container.cpp:432-509](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L432-L509)）：

- 先 `topological_sort`（[host/lib/experts/expert_container.cpp:437-438](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L437-L438)）。
- 若抛 `not_a_dag`（图有环），用 `cycle_det_visitor` 跑一次 DFS 把具体回路打印出来再抛 `runtime_error`（[host/lib/experts/expert_container.cpp:439-453](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L439-L453)）。
- 第一遍按拓扑序对脏节点 `resolve()`（[host/lib/experts/expert_container.cpp:466-498](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L466-L498)）。
- 第二遍把跑过的 worker `mark_clean()`（[host/lib/experts/expert_container.cpp:500-508](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L500-L508)）。

**两个排错利器**：

- `to_dot()`（[host/lib/experts/expert_container.cpp:128-161](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L128-L161)）：把整张图导出成 Graphviz DOT 文本（数据节点画椭圆、worker 画方框），可直接渲染成图。
- `debug_audit()`（[host/lib/experts/expert_container.cpp:163-271](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L163-L271)）：做三类静态检查——查环、查数据节点的入边/出边是否合理（多写者、不可达、未使用等）、查 worker 是否缺输入或输出。只在编译期打开 `UHD_EXPERT_LOGGING` 时生效。

#### 4.3.4 代码实践：导出 DOT 并运行官方测试

**实践目标**：亲眼看到 experts 图的可视化结果，并确认求解行为与断言一致。

**操作步骤**：

1. 在 [host/tests/expert_test.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/expert_test.cpp) 所有 worker 注册完成之后（约第 200 行之后），临时加一行（**示例代码**，仅用于观察，勿提交）：

   ```cpp
   std::cout << container->to_dot() << std::endl;
   ```

2. 若本地已构建 host，编译并运行 `expert_test`（通常为 `ctest -R expert_test`，或直接跑编译产物）。把 DOT 输出存为 `graph.dot`，用 `dot -Tsvg graph.dot -o graph.svg` 渲染。
3. 若无构建环境，则改为**源码阅读型实践**：对照 4.1.4 你手画的依赖图，与 `to_dot()` 的逻辑（[host/lib/experts/expert_container.cpp:128-161](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L128-L161)）核对，预测 DOT 里会有几个椭圆（数据节点）、几个方框（worker）。

**需要观察的现象**：渲染出的图应包含 7 个数据节点（`A/desired`、`B`、`C`、`D`、`E`、`F`、`G`）和 5 个有效 worker（worker1~worker5；worker6 是 `null_worker`，无输入无输出）。注意 `worker5` 的 `_c` 因未 `bind_accessor` 而不出现在图里。

**预期结果**：测试通过；图中边的方向与你手画的依赖图一致。

> 待本地验证：渲染与运行结果取决于本地是否已配置 Boost.Graph 与编译 host 测试。

#### 4.3.5 小练习与答案

**练习 1**：拓扑排序失败（图有环）时，框架如何帮开发者定位问题？

**答案**：`_resolve_helper` 捕获 `boost::not_a_dag`，再用 `cycle_det_visitor` 跑 DFS 收集所有「回边（back edge）」，把 `源→目标` 名字拼进异常信息抛出（[host/lib/experts/expert_container.cpp:439-453](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L439-L453)）。开发者据此就能知道是哪几个节点构成了环。

**练习 2**：`resolve_all(false)` 和 `resolve_all(true)` 的区别？为什么默认 `false`？

**答案**：`force=false` 只 resolve 脏节点，`force=true` 忽略脏标记强制 resolve 全部（[host/lib/experts/expert_container.cpp:480](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/experts/expert_container.cpp#L480)）。默认 `false` 是为了利用脏标记做增量求解——绝大多数情况下只有少量值变化，跳过未变化的节点能省掉大量重复计算。测试里 [host/tests/expert_test.cpp:260](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/expert_test.cpp#L260) 的 `resolve_all(true)` 是为了在强制重算后验证一致性。

---

## 5. 综合实践：追踪一条真实的属性传播链路

**任务背景**：到目前为止我们用的都是教学用例 `expert_test.cpp`。现在去看一条**真实工程链路**——ZBX 子板的射频调谐。这条链路展示了 experts 框架在工业级代码里如何把「一个调谐频率请求」传播成十几个硬件配置。

**实践目标**：把本讲的三个模块（节点 / 工厂 / 容器）串起来，画出 ZBX 调谐频率的 experts 节点依赖图，并解释传播过程。

**操作步骤**：

1. **看 worker 的 resolve 实现**。阅读 [host/lib/usrp/dboard/zbx/zbx_expert.cpp:148-214](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/zbx/zbx_expert.cpp#L148-L214) 的 `zbx_freq_fe_expert::resolve()`。这是一个「前端专家」：读入 `_desired_frequency`（用户想要的频率），裁剪到合法范围，查调谐表 `_tune_table`，然后写出一串输出：`_tune_settings`、`_lo1_inj_side`/`_lo2_inj_side`、`_desired_lo1_frequency`/`_desired_lo2_frequency`、`_desired_if2_frequency`、`_rf_filter`/`_if1_filter`/`_if2_filter`、`_band_inverted` 等。

2. **看下游 worker 如何消费**。阅读 [host/lib/usrp/dboard/zbx/zbx_expert.cpp:217-238](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/zbx/zbx_expert.cpp#L217-L238) 的 `zbx_freq_be_expert::resolve()`（后端专家）：它读 `_coerced_lo2_frequency`、`_coerced_if2_frequency`（注意是 **coerced**，即 LO 专家已经把 desired 矫正过的实际值），算出最终的 `_coerced_frequency`（回给用户的实际射频频率）。

3. **看节点与 worker 如何注册**。阅读 [host/lib/usrp/dboard/zbx/zbx_dboard_init.cpp:338-376](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/zbx/zbx_dboard_init.cpp#L338-L376)，能看到 `zbx_freq_be_expert`、`zbx_band_inversion_expert`、`zbx_rfdc_freq_expert`、`zbx_lo_expert`、`zbx_freq_fe_expert` 等一串 worker 被 `add_worker_node` 注册进容器，以及大量 `add_dual_prop_node<double>` 注册 desired/coerced 频率节点。

4. **画出依赖图**。把上面读到的关系画成节点依赖图（核心部分示意）：

   ```
   desired_frequency ─→ freq_fe_expert ─→ desired_lo1_freq ─→ lo_expert ─→ coerced_lo1_freq ─┐
                                       └→ desired_lo2_freq ─→ lo_expert ─→ coerced_lo2_freq ─┤
                                       └→ desired_if2_freq ─→ rfdc_freq_expert ─→ coerced_if2_freq ─┤
                                       └→ band_inverted ─→ band_inversion_expert (写硬件 IQ swap)    │
                                                                                                       │
                          coerced_lo2_freq + coerced_if2_freq ─→ freq_be_expert ─→ coerced_frequency
   ```

5. **解释传播**。当用户 `set_rx_frequency(f)`：property_tree 的 desired 订阅触发 `desired_frequency` 节点 `commit` → 因 auto-resolve 触发一次 `resolve_all` → 拓扑排序后依次跑 `freq_fe_expert`（算出各 LO/IF desired 值）→ `lo_expert`（把 desired LO 写进硬件、回读 coerced LO）→ `rfdc_freq_expert`（设 NCO、回读 coerced IF2）→ `freq_be_expert`（用所有 coerced 值算出最终 coerced_frequency）→ `band_inversion_expert`（写 IQ 交换）→ 一系列 programming expert 把滤波器/天线开关写进 CPLD。

**需要观察的现象**：

- 注意 `freq_be_expert` 读的是 **coerced** 系列（硬件实际值），而不是 desired——这保证回给用户的频率是硬件真正产生的频率，而不是用户请求的值（呼应 u2-l4「读回的 coerced 可能与请求的 desired 不同」）。
- 注意 `lo_expert::resolve()`（[host/lib/usrp/dboard/zbx/zbx_expert.cpp:240-255](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/zbx/zbx_expert.cpp#L240-L255)）里对每个输入都先 `is_dirty()` 判断再写硬件——这是脏标记在真实代码里省下不必要硬件访问的体现。

**预期结果**：你能用一段话讲清「一次 `set_rx_frequency` 如何沿 experts DAG 自动传播到 LO/滤波器/频带反转/CPLD」，并指出 desired 链路与 coerced 链路的分界。

> 待本地验证：本实践为源码阅读型，无需硬件；若手边有 ZBX 设备，可用 `uhd_usrp_probe` 与一次 `set_rx_freq` 配合抓 UHD 日志观察 expert 触发顺序（需开启 `UHD_EXPERT_LOGGING`）。

---

## 6. 本讲小结

- experts 框架把「一个配置变化引发的一串派生计算」建模成**有向无环图（DAG）**：数据节点存值、worker 节点算值、reader/writer 访问器隐式建边，靠拓扑排序保证求解顺序正确。
- **脏标记**是增量求解的核心：值未变就不传播（`dirty_tracked` 的赋值在 `!=` 时才置脏），`resolve_all` 只跑脏节点，第二遍才 `mark_clean`。
- `expert_factory` 是改图的唯一入口（友元），它的核心价值是**把数据节点的 `commit`/`retrieve` 绑成 property_tree 的回调**，从而让 experts 图与 u2-l4 的 desired/coerced 双值模型无缝对接：`add_dual_prop_node` 用 `MANUAL_COERCE` 把矫正权交给 worker。
- 四种 `auto_resolve_mode` 控制何时触发求解；但 `resolve_from`/`resolve_to` 当前都被覆盖为完整 `resolve_all`，所以模式只影响「触发时机」不影响「求解范围」。
- `expert_container` 用 Boost Graph Library 存图，`topological_sort` 驱动求解，有环时报具体回边；`to_dot()` 与 `debug_audit()` 是排错利器。
- **重要边界**：本讲的 experts 框架（`uhd/experts/`，用于子板 dboard 层的射频派生计算）与 RFNoC 流图 `commit()` 里的 `resolve_all_properties()`（`host/lib/rfnoc/graph.cpp`，用于块间属性传播）是**两套独立机制**，`host/lib/rfnoc` 里完全不引用 `expert_container`。不要把两者混淆。

---

## 7. 下一步学习建议

- **横向对比 RFNoC 属性传播**：阅读 `host/lib/rfnoc/graph.cpp` 里的 `resolve_all_properties`，对比它与 experts 框架在「图模型、脏机制、求解触发」上的异同，加深对「为什么有两套」的理解。
- **深入一个真实 expert 容器**：以 ZBX 为样板，通读 [host/lib/usrp/dboard/zbx/zbx_dboard_init.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/zbx/zbx_dboard_init.cpp) 的整张建图代码，把综合实践里的依赖图补全到包含 gain expert、programming expert、sync expert。
- **回到测试**：把 `host/tests/expert_test.cpp` 当作沙盒，尝试新增一个 worker（例如 `H = G + 1`）并加对应数据节点，用 `to_dot()` 验证你的图，用断言验证传播。
- **下一讲 u3-l6（常用 RFNoC 块）**：本讲聚焦子板层的 experts 计算，下一讲回到 RFNoC 块（Radio/DDC/DUC/FFT/Replay），届时可带着「块层属性传播是另一套机制」的认知去读 DDC/DUC 的采样率配置。
