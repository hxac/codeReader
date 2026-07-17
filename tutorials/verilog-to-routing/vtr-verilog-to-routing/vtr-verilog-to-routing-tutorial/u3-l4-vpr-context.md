# VprContext 与全局状态管理

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 VPR 为什么要有一个全局的 `VprContext`，它解决了什么问题。
- 列举 `VprContext` 聚合的各个子上下文（Device / Atom / Clustering / Placement / Routing / Timing / Power / NoC / Floorplanning 等），以及各自负责保管哪些数据。
- 理解「mutable / immutable getter」这一访问模式：为什么默认只给只读引用，需要写才显式取 mutable 引用。
- 解释为什么所有 Context 都被设计成「不可拷贝」，以及全局访问器 `g_vpr_ctx` 是怎么声明和定义的。
- 在真实源码中定位上述机制，并能写出一段符合规范的状态访问代码（阅读型实践）。

本讲是「核心数据结构与上下文体系」单元的关键一讲。前面 u3-l1~u3-l3 已经讲了 `Netlist`、`AtomNetlist`、`ClusteredNetlist` 这些具体网表；本讲要回答的是：**这些网表以及器件、布局、布线、时序等大大小小的状态，到底被统一放在哪里、又由谁来管？** 答案就是 `VprContext`。

---

## 2. 前置知识

在进入本讲前，建议你已经具备以下概念（前序讲义已建立）：

- **数据流主线**：VPR 内部数据沿 `AtomNetlist → Prepacker → Packer → ClusteredNetlist → Placer → Router → Timing` 一路流转。每个阶段既是上一阶段的消费者，又是下一阶段的生产者（见 u1-l3、u3-l2、u3-l3）。
- **C++ 引用与 `const`**：本讲大量出现 `const T&`（只读引用）与 `T&`（可写引用）。如果你对 `const` 正确性（const-correctness）不熟，记住一句话：**能加 `const` 就加 `const`，编译器会帮你拦下意外的修改。**
- **聚合（aggregation）**：一个类把若干其它对象作为成员持有，叫聚合。`VprContext` 就是把一堆子上下文聚合起来的「大袋子」。
- **全局变量**：用 `extern` 声明、在某个 `.cpp` 中定义一次、全程序可见的变量。本讲会看到 VPR 用一个全局 `g_vpr_ctx` 作为单例。

> 不熟悉 FPGA 术语也没关系。本讲重点是「状态管理」这一个软件工程问题，FPGA 概念只作为例子出现，会随用随解释。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [`vpr/src/base/vpr_context.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h) | 本讲主角。定义了 `Context` 不可拷贝基类、十余个子上下文结构体，以及把它们聚合起来的 `VprContext` 类。 |
| [`vpr/src/base/globals.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/globals.h) | 用 `extern` 声明全局访问器 `g_vpr_ctx`，供全工程引用。 |
| [`vpr/src/base/globals.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/globals.cpp) | 真正定义（分配）这个全局对象的唯一一处。 |
| [`vpr/src/base/vpr_types.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h) | 提供子上下文里用到的各种 POD 类型（如 `t_block_loc`、`e_pad_loc_type`、`e_gsb_version` 等）。 |
| [`vpr/src/base/vpr_api.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) | 主流程编排文件，里面有大量 `g_vpr_ctx.xxx()` 的真实使用范例，用来验证访问模式。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **子上下文职责划分**——`VprContext` 里到底分了哪些「抽屉」，每个抽屉装什么。
2. **mutable / immutable getter 模式**——为什么读和写要走两套不同的取值函数。
3. **全局访问器 `g_vpr_ctx` 与不可拷贝基类**——这个大袋子怎么被全工程拿到手，以及为什么禁止拷贝。

---

### 4.1 子上下文职责划分

#### 4.1.1 概念说明

VPR 是一个多阶段流水线：打包、布局、布线、时序分析……每个阶段都会产生或消费一大堆数据结构——器件网格、原子网表、聚簇网表、布局坐标、布线树、时序图等等。如果把这些东西零散地散落在各自的全局变量里，会出现两个问题：

- **耦合失控**：任意一个 `.cpp` 都能直接碰任意状态，改动时根本不知道谁会受影响。
- **理解困难**：新人面对几十个全局变量，无从下手。

VPR 的解法是**按主题分组**：把「逻辑上属于同一类」的数据结构收进一个子上下文（sub-context）。例如所有描述「目标器件物理形态」的（网格、瓦片类型、布线资源图）都进 `DeviceContext`；所有描述「原子级网表」的进 `AtomContext`；所有描述「当前布局」的进 `PlacementContext`。最后再用一个顶层类 `VprContext` 把所有子上下文聚合起来。

这样一来，「主题」就成了一种天然的命名空间和访问边界：你想看布局，就只去 `placement()`；想看器件，就只去 `device()`。

#### 4.1.2 核心流程

可以从两个维度理解子上下文的划分：

```
                 ┌──────────────────────── VprContext ────────────────────────┐
                 │                                                            │
   静态/只读  →  │  DeviceContext (器件：解析后基本不变)                        │
                 │  AtomContext   (原子网表：综合后基本不变)                    │
                 │  TimingContext (时序图与约束：构建后基本不变)                │
                 │                                                            │
   动态/可变  →  │  ClusteringContext (聚簇网表：打包阶段产生)                  │
                 │  PlacementContext (布局：布局阶段不断变化)                   │
                 │  RoutingContext   (布线：布线阶段不断变化)                   │
                 │                                                            │
   按需启用   →  │  PowerContext / FloorplanningContext / NocContext / ...     │
                 └────────────────────────────────────────────────────────────┘
```

- **纵向**：按 CAD 主题分（device / atom / clustering / placement / routing / timing / power / noc / floorplanning …）。
- **横向（生命周期）**：有些子上下文在流水线早期就填好、之后几乎只读（如 device、atom）；有些在一个阶段内被反复改写（如 placement、routing）；有些只在特定功能开启时才有意义（如 power 要功耗分析、noc 要片上网络、server 要服务端模式）。

#### 4.1.3 源码精读

`VprContext` 这个聚合类本身非常干净，几乎全是「一对 getter + 一个私有成员」的重复，见 [vpr/src/base/vpr_context.h:869-925](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L869-L925)。节选关键部分：

```cpp
class VprContext : public Context {
  public:
    const AtomContext& atom() const { return atom_; }
    AtomContext& mutable_atom() { return atom_; }

    const DeviceContext& device() const { return device_; }
    DeviceContext& mutable_device() { return device_; }
    // ... timing / power / clustering / placement / routing
    // ... floorplanning / noc / packing_multithreading / server ...
  private:
    DeviceContext device_;
    AtomContext atom_;
    TimingContext timing_;
    // ... 其余子上下文成员 ...
};
```

这段代码说明：`VprContext` 自身也继承自 `Context`，内部把每个子上下文作为**值成员**持有（不是指针），生命周期与 `VprContext` 完全一致。

再看每个子上下文装了什么。三个最常被引用的：

**`AtomContext`**（原子级网表状态），见 [vpr_context.h:87-125](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L87-L125)。核心私有成员：

```cpp
struct AtomContext : public Context {
  private:
    AtomNetlist nlist_;              // 原子网表（u3-l2 讲过）
    AtomLookup lookup_;              // 原子实体与后续阶段实体的映射
    FlatPlacementInfo flat_placement_info_; // 打包前的扁平布局信息
  public:
    const AtomNetlist& netlist() const { return nlist_; }       // 只读
    AtomNetlist& mutable_netlist() { return nlist_; }           // 可写
    // ...
};
```

注意它把数据放进 `private`，再通过 getter 暴露——比下面的 `DeviceContext` 更「封装」，因为它有自己的 mutable/immutable 约定（见 4.2）。

**`TimingContext`**（时序分析状态），见 [vpr_context.h:133-156](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L133-L156)。成员是公开的（`struct` 默认 public）：

```cpp
struct TimingContext : public Context {
    std::shared_ptr<tatum::TimingGraph> graph;       // 时序图（节点=引脚，边=依赖）
    std::shared_ptr<tatum::TimingConstraints> constraints; // SDC 约束
    t_timing_analysis_profile_info stats;
    bool terminate_if_timing_fails = false;
};
```

它用 `std::shared_ptr` 持有外部库 Tatum 的图与约束——这是因为时序图体积大、且可能被多处共享，用智能指针便于重建与生命周期管理。

**`DeviceContext`**（目标器件状态）是体量最大的子上下文，见 [vpr_context.h:163-365](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L163-L365)。它装着：

- `DeviceGrid grid;`——器件二维/三维网格（u2-l3 讲过）。
- `std::vector<t_physical_tile_type> physical_tile_types;` 与 `logical_block_types;`——物理瓦片与逻辑块类型集合（u2-l2 讲过）。
- `RRGraphBuilder rr_graph_builder;` 与 `RRGraphView rr_graph;`——布线资源图的可写视图与只读视图（u6-l1 会深入）。
- `const t_arch* arch;`——指向架构 XML 解析结果的顶层指针。
- 一堆 `chan_width`、`rr_indexed_data`、开关信息等。

其余子上下文职责一览（都在同一个头文件里，可自行跳转）：

| 子上下文 | 行号 | 保管的内容 |
| --- | --- | --- |
| `ClusteringContext` | [395-417](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L395-L417) | 打包后的 `ClusteredNetlist clb_nlist` 及簇内引脚到网的映射 |
| `PlacementContext` | [435-569](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L435-L569) | 块坐标 `blk_loc_registry_`、布局宏、压缩网格等 |
| `RoutingContext` | [577-638](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L577-L638) | 路由树 `route_trees`、节点路由信息、前瞻缓存等 |
| `PowerContext` | [373-387](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L373-L387) | 功耗估算的解信息、工艺数据、按组件分解 |
| `FloorplanningContext` | [646-723](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L646-L723) | 布局规划约束（原子/簇的区域限制） |
| `NocContext` | [731-764](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L731-L764) | 片上网络模型、流量、路由算法 |
| `ServerContext` | [773-818](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L773-L818) | server 模式下的 GateIO、TaskResolver、关键路径渲染状态 |

> 提示：`ServerContext` 被包在 `#ifndef NO_SERVER` 里（[766 行](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L766) 与 [901 行](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L901)），编译时若定义了 `NO_SERVER`，server 相关状态会被整体裁掉。这是条件编译在状态管理上的典型用法。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，建立「子上下文 → 关键成员」的心智地图。

**操作步骤**：

1. 打开 [vpr_context.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h)。
2. 用编辑器的「查找符号」依次跳到 `DeviceContext`、`AtomContext`、`TimingContext`。
3. 对每个子上下文，挑出 2~3 个你认为「最能代表这个主题」的成员。

**需要观察的现象**：

- `DeviceContext` 的成员大多与「芯片物理」相关（grid、tile types、rr_graph）。
- `AtomContext` 把成员放成了 `private`，与 `DeviceContext`/`TimingContext`（`struct` 默认 public）风格不同。
- `TimingContext` 的两个核心成员都是 `std::shared_ptr`。

**预期结果**：你能不查资料地说出「要看器件物理 → `device()`」「要看原子网表 → `atom().netlist()`」「要看时序图 → `timing().graph`」。

> 运行结果：本实践为源码阅读型，无需运行命令，无确定运行结果。

#### 4.1.5 小练习与答案

**练习 1**：`VprContext` 把子上下文作为「值成员」而非「指针成员」持有，这有什么好处？

> **答案**：值成员意味着子上下文的生命周期与 `VprContext` 完全绑定，`VprContext` 构造时它们就地构造、析构时自动析构，无需手动 `new/delete`，也避免了悬垂指针。由于全程序只有一个 `VprContext` 实例（`g_vpr_ctx`），值持有的额外开销可以忽略。

**练习 2**：如果要做功耗分析，应该去哪个子上下文找数据？如果没开功耗分析，那个子上下文还存在吗？

> **答案**：去 `PowerContext`（`g_vpr_ctx.power()`）。子上下文作为值成员始终存在（不像 server 那样被条件编译裁剪），只是未开功耗分析时里面是空/默认状态。

---

### 4.2 mutable / immutable getter 模式

#### 4.2.1 概念说明

`VprContext` 有一个非常显眼的设计：每个子上下文都配**两个** getter——一个返回 `const T&`（只读/immutable），一个返回 `T&`（可写/mutable），后者名字统一加 `mutable_` 前缀。

为什么要这么做？因为 VPR 的状态绝大部分时候是**被读取**的（算法要反复查器件、查网表、查布局），真正去**修改**全局状态的只有少数几个阶段的写者。如果所有 getter 都返回可写引用，那么任何手滑的赋值、任何意外的修改，编译器都不会报错——在一个几十万行的工程里，这是巨大的隐患。

通过默认提供只读引用、把可写引用「藏」在带 `mutable_` 前缀的名字后面，VPR 让「读」成为下意识的默认动作，让「写」变成一个需要刻意选择的动作。一旦你写 `auto& x = g_vpr_ctx.device();`（漏了 `mutable_`，且后面试图改 `x`），编译器会用 `const` 错误拦住你。这是 C++ const-correctness 在大型项目里的典范应用。

#### 4.2.2 核心流程

访问 VPR 状态的标准动作只有两种：

```text
读：auto& x = g_vpr_ctx.<主题>();          // 返回 const T&，编译期禁止改
写：auto& x = g_vpr_ctx.mutable_<主题>();  // 返回 T&，允许改
```

判断该用哪个，问自己一句话：**「我接下来会改它吗？」** 会 → `mutable_`；不会 → 普通 getter。能不加 `mutable_` 就不加，这是 VPR 的编码约定。

进一步，`AtomContext`、`PlacementContext` 还在自己的成员上**再套一层** mutable/immutable getter（例如 `netlist()` vs `mutable_netlist()`），形成两层防线。`PlacementContext` 更进一步用 `lock_loc_vars()` / `unlock_loc_vars()` 在布局阶段把布局坐标「锁起来」，防止布局代码误碰全局副本。

#### 4.2.3 源码精读

顶层两套 getter，见 [vpr_context.h:871-890](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L871-L890)：

```cpp
const AtomContext& atom() const { return atom_; }
AtomContext& mutable_atom() { return atom_; }

const DeviceContext& device() const { return device_; }
DeviceContext& mutable_device() { return device_; }
```

> 注意：连 `atom()` 这个只读 getter 自己也被声明为 `const` 成员函数（行尾的 `const`），意味着「在 `const VprContext` 上也能调用它」。这保证了不可变场景下的链式只读访问。

子上下文内部的第二层 getter，以 `AtomContext` 为例，见 [vpr_context.h:104-108](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L104-L108)：

```cpp
inline const AtomNetlist& netlist() const { return nlist_; }
inline AtomNetlist& mutable_netlist() { return nlist_; }
```

现在看真实使用。在主流程 [vpr_api.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) 里能同时看到「读」和「写」两种写法。先看**只读**访问（只查询、不改），位于 [vpr_api.cpp:369](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L369)：

```cpp
if (g_vpr_ctx.routing().constraints.get_num_route_constraints() && ...) { ... }
```

这里用的是 `.routing()`（只读），符合「只是查一下路由约束数量」的语义。

再看**可写**访问（要往里填数据），位于 [vpr_api.cpp:376](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L376)：

```cpp
auto& device_ctx = g_vpr_ctx.mutable_device();
```

以及混合读取多个上下文的情况，位于 [vpr_api.cpp:472-474](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L472-L474)，同时从 clustering 与 placement 取只读引用：

```cpp
g_vpr_ctx.clustering().clb_nlist,
g_vpr_ctx.placement().block_locs(),
g_vpr_ctx.clustering().atoms_lookup
```

还有一个绝佳的对照例子——同一函数里，对 clustering 取**可写**、对 atom 取**只读**，位于 [vpr_api.cpp:787-788](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L787-L788)：

```cpp
auto& cluster_ctx = g_vpr_ctx.mutable_clustering();      // 要写：产生聚簇网表
const AtomContext& atom_ctx = g_vpr_ctx.atom();          // 只读：消费原子网表
```

这一行是整个模式最精炼的缩影：**生产者取 mutable，消费者取 const。**

最后看 `PlacementContext` 的额外保险机制——布局坐标的加锁/解锁，见 [vpr_context.h:509-530](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L509-L530)：

```cpp
void lock_loc_vars() {
    VTR_ASSERT_SAFE(loc_vars_are_accessible_);
    loc_vars_are_accessible_ = false;   // 进入布局阶段：锁住
}
void unlock_loc_vars() {
    VTR_ASSERT_SAFE(!loc_vars_are_accessible_);
    loc_vars_are_accessible_ = true;    // 布局结束：解锁
}
```

每个布局坐标 getter（如 `block_locs()`）内部都带 `VTR_ASSERT_SAFE(loc_vars_are_accessible_)`（见 [vpr_context.h:476-479](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L476-L479)）。也就是说，布局进行中若有人想从全局上下文读布局坐标，断言会触发——因为此时坐标是「半成品」，不该被外部看到。这是 mutable/immutable 之外的第二道运行期防线。

#### 4.2.4 代码实践

**实践目标**：在真实调用点体会「该用 `mutable_` 还是普通 getter」。

**操作步骤**：

1. 打开 [vpr_api.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp)。
2. 搜索 `g_vpr_ctx.`（全文有几十处）。
3. 把每一处分成两类：带 `mutable_` 的（写）、不带的（读）。
4. 对每一处，从函数名和上下文判断「为什么这里是写/读」。

**需要观察的现象**：

- 写操作几乎都集中在各阶段的「入口」（如 `vpr_pack_flow` 里写 clustering，[vpr_api.cpp:787](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L787)）。
- 读操作遍布各处，且很多函数从头到尾只用普通 getter。

**预期结果**：你会发现「写」的次数远少于「读」，这正是这套模式的价值——把稀缺、危险的写操作显式化。

> 运行结果：本实践为源码阅读型，无需运行命令，无确定运行结果。

#### 4.2.5 小练习与答案

**练习 1**：假设你写了 `auto& d = g_vpr_ctx.device();`，然后 `d.chan_width.x_max = 100;`，会发生什么？

> **答案**：编译失败。`device()` 返回的是 `const DeviceContext&`，对其成员赋值违反 `const` 正确性。你必须改用 `g_vpr_ctx.mutable_device()` 才能编译——这正是该模式要的效果：强迫你显式确认「我确实要改全局器件状态」。

**练习 2**：`PlacementContext` 为什么在 mutable/immutable 之外还要加 `lock_loc_vars()`？

> **答案**：`const` 只能在编译期防止「误改」，但挡不住「布局进行中读取半成品坐标」这种逻辑错误。布局阶段坐标在不断变化，若此时别的代码从全局读布局坐标会拿到不一致的数据。`lock_loc_vars()` 在运行期用断言把布局坐标「封存」，确保只有布局结束后（`unlock_loc_vars()`）外部才能读，补上了编译期检查管不到的那一层。

---

### 4.3 全局访问器 g_vpr_ctx 与不可拷贝基类

#### 4.3.1 概念说明

有了 `VprContext` 这个大袋子，下一个问题是：**全工程怎么拿到它？**

VPR 选择了最直接的方式——一个全局变量 `g_vpr_ctx`。任何 `.cpp` 只要 `#include "globals.h"`，就能用 `g_vpr_ctx.device()`、`g_vpr_ctx.mutable_placement()` 等等。这是 VPR 内部约定俗成的「唯一真相来源（single source of truth）」。

但全局变量有个经典隐患：**被意外按值拷贝**。`VprContext` 体积极其巨大（光 `DeviceContext` 里就有网格、布线图、上百兆数据），如果有人写出 `VprContext local_copy = g_vpr_ctx;` 或函数按值传参 `void f(VprContext ctx)`，会触发一次灾难性的深拷贝——既慢得出奇，也会造成两份状态不一致（改了副本没改原件）。

VPR 用一个精巧的基类 `Context` 一劳永逸地堵死了这条路：它把拷贝构造和拷贝赋值**删除**（`= delete`）。所有子上下文与 `VprContext` 都继承自它，于是统统不可拷贝。这样一旦有人试图按值使用，直接编译报错，问题在编译期就被消灭。

#### 4.3.2 核心流程

机制由三层配合：

```text
① Context 基类：delete 掉拷贝构造/拷贝赋值  →  所有子上下文不可拷贝
        ▲ 继承
② VprContext：聚合各子上下文（值成员）      →  因成员不可拷贝，整体也不可拷贝
        ▲ extern 声明 / 全局定义
③ g_vpr_ctx：全程序唯一实例                 →  必须「按引用」使用，按值即编译错误
```

实际的取用方式只有一种：**取引用**。

```cpp
auto& place_ctx = g_vpr_ctx.placement();         // 正确：按 const 引用
auto& place_ctx = g_vpr_ctx.mutable_placement(); // 正确：按可写引用
// VprContext bad = g_vpr_ctx;                   // 编译错误：不可拷贝
// void f(VprContext ctx);                       // 编译错误：按值传参也不行
```

#### 4.3.3 源码精读

**第一层：不可拷贝基类 `Context`**，见 [vpr_context.h:68-74](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L68-L74)：

```cpp
struct Context {
    //Contexts are non-copyable
    Context() = default;
    Context(Context&) = delete;            // 禁止拷贝构造
    Context& operator=(Context&) = delete; // 禁止拷贝赋值
    virtual ~Context() = default;
};
```

注意它的注释强调：**这个基类唯一的目的就是禁止拷贝**，不应在里面放任何数据或方法。`virtual ~Context()` 给了它多态析构能力（虽然这里主要用意在禁拷贝）。

**第二层：`VprContext` 聚合**，见 [vpr_context.h:869](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L869)（`class VprContext : public Context`）。因为它的私有成员（`DeviceContext device_;` 等）都不可拷贝，编译器合成的拷贝构造/赋值会被自动删除——即便 `VprContext` 自己什么都不写，也是天然不可拷贝。

**第三层：全局访问器声明与定义**。声明在 [globals.h:9](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/globals.h#L9)：

```cpp
extern VprContext g_vpr_ctx;
```

这一行告诉所有包含 `globals.h` 的文件：「有一个名叫 `g_vpr_ctx` 的 `VprContext` 对象存在，链接时能找到。」真正的定义（分配内存、调用构造）只在 [globals.cpp:4](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/globals.cpp#L4) 出现一次：

```cpp
VprContext g_vpr_ctx;
```

这是 C++ 全局对象的经典三件套：头文件 `extern` 声明 → 一个 `.cpp` 定义 → 全工程按名引用。整个 `globals.cpp` 加上 include 也只有 5 行（见 [globals.cpp:1-5](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/globals.cpp#L1-L5)），极简。

至于子上下文里用到的那些 POD 类型（如 `t_block_loc`、`e_pad_loc_type`、`e_gsb_version`），它们来自 [vpr_types.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h)，例如 [`t_block_loc`（660 行起）](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L660)、[`e_pad_loc_type`（917 行）](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L917)、[`e_gsb_version`（1451 行）](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1451)。`vpr_context.h` 通过 `#include "vpr_types.h"`（[第 14 行](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L14)）把它们引入。`vpr_types.h` 本身只提供「零件」，不提供状态管理。

#### 4.3.4 代码实践

**实践目标**：亲手验证「不可拷贝」与「全局唯一实例」两件事。

**操作步骤**：

1. 打开 [vpr_context.h:68-74](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L68-L74)，确认 `Context` 删除了两个拷贝操作。
2. 打开 [globals.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/globals.cpp)，确认全工程只有这一处定义了 `g_vpr_ctx`。
3. 在仓库里搜索 `extern VprContext g_vpr_ctx`，确认只有 `globals.h` 做了声明（不会重复定义）。
4. （可选，需要构建环境）写一个最小 `.cpp` 包含 `globals.h`，尝试 `VprContext dup = g_vpr_ctx;`，观察编译器报错。

**需要观察的现象**：

- `Context` 基类没有任何数据成员，只有构造/析构/被删除的拷贝操作。
- `globals.cpp` 没有任何初始化逻辑——`g_vpr_ctx` 的各子上下文都走默认构造，内容由后续阶段填充。

**预期结果**：第 4 步若执行，会看到类似「deleted function」「cannot be referenced — it is a deleted function」的编译错误，证明禁拷贝生效。

> 运行结果：步骤 1~3 为源码阅读型，无确定运行结果；步骤 4 若本地有构建环境可验证，否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果 `VprContext` 不继承 `Context`、也不自己删除拷贝操作，它还能被拷贝吗？

> **答案**：通常仍不能，但「原因不同」。因为它的成员（如 `DeviceContext device_`）继承自 `Context` 而不可拷贝，编译器合成的 `VprContext` 拷贝构造会因成员不可拷贝而被自动删除。继承 `Context` 的好处是把这一约定**显式化、统一化**——所有子上下文直接继承就自动获得禁拷贝，不必每个都手写 `= delete`。

**练习 2**：为什么 `g_vpr_ctx` 用全局变量，而不是把 `VprContext` 作为参数在各函数间传递？

> **答案**：纯粹是工程权衡。VPR 几乎所有算法都要访问 device/atom/clustering 等状态，若改成传参，每个函数签名都要挂一长串引用参数，调用链改一处牵动全身，可读性会大幅下降。用全局变量换取了调用简洁性，代价是状态可见性放宽——VPR 用 mutable/immutable getter、断言锁、不可拷贝等多重约束来缓解这一代价。这是一种「务实优先」的设计选择，不是唯一正确的做法。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面的综合任务（即规格指定的实践任务）。

**任务**：在 [vpr_context.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h) 中，分别列出 `DeviceContext`、`AtomContext`、`TimingContext` 各自持有哪些关键成员，并说明为什么 `Context`（及其所有派生类）要禁止拷贝。

**建议步骤**：

1. 列成员。针对三个子上下文，各挑 2~3 个最有代表性的成员，填入下表（示例答案已给出，请到源码核对行号）：

   | 子上下文 | 关键成员 | 含义 | 源码位置 |
   | --- | --- | --- | --- |
   | `DeviceContext` | `DeviceGrid grid` | 器件网格 | [175](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L175) |
   | `DeviceContext` | `RRGraphView rr_graph` | 布线资源图只读视图 | [240](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L240) |
   | `DeviceContext` | `const t_arch* arch` | 架构解析结果顶层指针 | [344](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L344) |
   | `AtomContext` | `AtomNetlist nlist_` | 原子级网表 | [93](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L93) |
   | `AtomContext` | `AtomLookup lookup_` | 原子实体↔阶段实体映射 | [95](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L95) |
   | `TimingContext` | `std::shared_ptr<tatum::TimingGraph> graph` | 时序图 | [143](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L143) |
   | `TimingContext` | `std::shared_ptr<tatum::TimingConstraints> constraints` | SDC 时序约束 | [150](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L150) |

2. 回答「为何禁止拷贝」。结合 [Context 基类（68-74 行）](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L68-L74) 与 [globals.cpp 的单实例定义](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/globals.cpp#L4)，从两个角度作答：
   - **性能**：`VprContext` 体积极大，一次深拷贝可能复制上百兆数据，且任何按值传参/返回都会触发。
   - **正确性**：VPR 依赖「单一真相来源」——所有阶段读写同一个 `g_vpr_ctx`。若被拷贝出副本，对副本的修改不会反映到原件，状态会分裂成多份、彼此不一致，是极难排查的 bug。
   - **结论**：禁拷贝把这两类问题消灭在编译期——任何按值使用直接报错，迫使所有人按引用访问唯一的 `g_vpr_ctx`。

3. （延伸）挑一个 [vpr_api.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) 中的真实调用点（如 [787-788 行](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L787-L788)），说明它对哪个子上下文取了 mutable、对哪个取了 const，并用「生产者/消费者」解释原因。

> 运行结果：本综合实践为源码阅读型，步骤 1~3 均无需运行命令，无确定运行结果；若想做禁拷贝编译验证，标注「待本地验证」。

---

## 6. 本讲小结

- `VprContext` 是 VPR 的**全局状态容器**，把按 CAD 主题划分的十余个子上下文（Device / Atom / Clustering / Placement / Routing / Timing / Power / Floorplanning / Noc / Server 等）聚合为一，是所有阶段共享状态的「单一真相来源」。
- 每个子上下文对应流水线的一类数据：`DeviceContext` 装器件物理（网格、瓦片、布线图），`AtomContext` 装原子网表，`TimingContext` 装时序图与约束，`PlacementContext`/`RoutingContext` 装布局/布线的动态结果。
- 访问遵循 **mutable / immutable 双 getter 模式**：默认 `xxx()` 返回 `const T&`（只读），需要写才用 `mutable_xxx()` 返回 `T&`；这是用 C++ const-correctness 把「读」设为默认、把「写」显式化的典范。
- `PlacementContext` 还用 `lock_loc_vars()`/`unlock_loc_vars()` 加了**运行期断言锁**，防止布局进行中读取半成品坐标，补足编译期检查管不到的逻辑层。
- 全工程通过全局访问器 `g_vpr_ctx`（`globals.h` 声明、`globals.cpp` 唯一定义）取用，且**必须按引用**使用。
- 所有 Context 不可拷贝：基类 `Context` 用 `= delete` 删除拷贝构造/赋值，连带所有派生类与 `VprContext` 都不可拷贝，把「按值深拷贝巨型状态」与「状态分裂」两类隐患消灭在编译期。

---

## 7. 下一步学习建议

- **衔接 u3-l5（主流程编排 vpr_api）**：本讲多次引用 `vpr_api.cpp`，下一篇会系统讲解 `vpr_init / vpr_flow / vpr_free_all` 如何按阶段顺序填充与消费 `g_vpr_ctx` 的各子上下文——那是把「状态容器」和「阶段流水线」真正连起来的主动脉。
- **回看数据流**：带着本讲的「子上下文」视角，重读 u3-l2（`AtomContext` 里的 `AtomNetlist`）与 u3-l3（`ClusteringContext` 里的 `ClusteredNetlist`），你会更清楚这些网表「住在哪个抽屉里」。
- **进阶阅读**：随后的单元会深入各子上下文的内部——u4 打包（ClusteringContext 的生产者）、u5 布局（PlacementContext + lock 机制）、u6 布线（RoutingContext + DeviceContext 的 `rr_graph`）、u7 时序（TimingContext + Tatum）。读这些章节时，留意每个阶段入口「取 mutable、产出后供后续只读」的固定节奏。
- **源码延伸**：想看条件编译如何裁剪状态，可对比 `#ifndef NO_SERVER` 包裹的 `ServerContext`；想看状态如何被清理，可在 `vpr_api.cpp` 中搜索 `vpr_free_all` 相关释放逻辑（注意：本讲未展开释放细节，留给 u3-l5）。
