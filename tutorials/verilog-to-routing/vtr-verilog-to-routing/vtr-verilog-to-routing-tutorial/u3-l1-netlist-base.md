# Netlist 泛型基类

## 1. 本讲目标

本讲是「核心数据结构与上下文体系」单元的第一讲，聚焦 VPR 中一切网表的共同祖先——`Netlist` 模板基类。

读者学完后应该能够：

- 说清楚 `Netlist` 用 **Block / Port / Pin / Net** 四类实体如何刻画一张电路网表，以及它们之间的指向关系。
- 理解 `vtr::StrongId` 这一「带标签的强类型 ID」为何能从编译期挡住「把 PinId 当 NetId 用」的低级错误。
- 看懂 `vtr::vector_map` 这个「以 ID 为下标的 vector」如何用结构数组（Struct-of-Arrays）兼顾内存紧凑与缓存友好。
- 能够在源码里定位四类实体的 ID 别名、主存储容器与交叉引用方法，并写出「由一个 Pin 找到它所属 Net」的伪代码。

本讲只讲**基类**本身。`AtomNetlist`（原子级网表）与 `ClusteredNetlist`（聚簇网表）如何继承并特化它，留给 u3-l2、u3-l3 两讲。

## 2. 前置知识

在进入源码前，先用日常语言铺垫三个概念。

### 2.1 什么是网表（Netlist）

把一块数字电路想象成一张图：

- **块（Block）** 是图里的节点，比如一个 LUT、一个触发器、一个加法器，或者一个 IO 引脚。
- **网（Net）** 是图里的边，表示「一根导线」。一根导线有一个**驱动端**（信号从哪来）和若干**接收端**（信号到哪去）。

把所有块和它们之间的连线记下来，就得到一张「网表」。VPR 后续的打包、布局、布线，本质上都是在不断改写这张网表——先把它从「原子级原语」抽象成「逻辑块级」，再为每个块在芯片上找个位置，最后为每根网找出一条物理通路。

### 2.2 为什么用 ID 而不用指针

表示「块 A 连到块 B」时，初学者常想用指针 `Block*`。但在 CAD 工具里，网表会被频繁增删、搬移、压缩：一旦某个块被搬走，所有指向它的指针都得更新，极易出错。

VPR 的做法是：给每个块/端口/引脚/网分配一个**整数 ID**（0、1、2、3……），所有「谁连到谁」都用 ID 表示。要查一个块的信息，就拿它的 ID 去一个数组里取。这样数据搬动时只需重排数组、更新一张「旧 ID → 新 ID」的映射表（本讲会看到 `NetlistIdRemapper`），客户端代码结构不变。

### 2.3 强类型（Strong Typing）要解决什么

如果所有 ID 都是裸 `int`，就会出这种 bug：

```cpp
size_t count_net_terminals(int net_id);
int blk_id = 10;
count_net_terminals(blk_id);  // 编译通过，但把块 ID 当成了网 ID —— 隐患！
```

`typedef int NetId;` 也救不了，因为 typedef 只是别名，`int`、`NetId`、`BlkId` 之间仍可隐式转换。`StrongId` 用一个**幽灵标签类型（phantom tag）**让「块 ID」和「网 ID」在编译器眼里是两种完全不同的类型，混用直接编译报错。这是本讲第二模块的核心。

> 与 u1-l5 的承接：u1-l5 讲过 VPR 命令行把所有选项汇聚成 `t_options` 聚合结构体；本讲的 `Netlist` 则是 VPR 内部「电路本身」的聚合结构体，二者都是「用强类型把一堆相关数据收拢」的设计思路。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vpr/src/base/netlist.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h) | `Netlist` 模板基类的声明。定义四类实体的查询/修改接口、交叉引用关系、数据成员（结构数组）。本讲主战场。 |
| [vpr/src/base/netlist.tpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.tpp) | `Netlist` 模板的实现（`.tpp` 是 template implementation 的后缀，被 `.h` 末尾 include）。交叉引用、建表等逻辑的真实代码在这里。 |
| [vpr/src/base/netlist_fwd.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist_fwd.h) | 基类用的四种「父 ID」别名（`ParentBlockId` 等）与 `PortType`/`PinType` 枚举的前向声明。 |
| [libs/libvtrutil/src/vtr_strong_id.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_strong_id.h) | `StrongId` 模板的定义与详细注释。类型安全 ID 的实现机制。 |
| [libs/libvtrutil/src/vtr_vector_map.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector_map.h) | `vector_map` 容器：一个「以 StrongId 为下标」的 vector，是 `Netlist` 所有主存储的底座。 |

## 4. 核心概念与源码讲解

### 4.1 四元 ID 模型：Block / Port / Pin / Net

#### 4.1.1 概念说明

`Netlist` 把一张电路拆成四类实体，每类实体都有自己的 ID 类型：

- **Block（块）**：网表里的基本节点，是「超图（hyper-graph）的顶点」。一个块有名字、类型，以及若干输入/输出/时钟端口。对应 `BlockId`。
- **Port（端口）**：块上一组（可能多位）引脚的逻辑分组。比如一个 N 位加法器，有两个 N 位输入端口、一个 N 位输出端口，共三个 Port。端口有位宽。对应 `PortId`。
- **Pin（引脚）**：单比特的连接点，把「一个块」和「一根网」连起来。Port 是多位的，每一位就是一个 Pin。对应 `PinId`。
- **Net（网）**：块与块之间的连线（超图的边）。每根网有**恰好一个驱动引脚**和**若干接收引脚**。对应 `NetId`。

这四者不是孤立的，而是彼此交叉引用：块知道自己的端口和引脚；端口知道它属于哪个块、含哪些引脚；引脚知道它属于哪个端口、连到哪根网；网知道它的驱动引脚和所有接收引脚。`netlist.h` 顶部用一张 ASCII 图精确画出了这些关系。

一个关键设计决策：**在整个网表范围内，每种 ID 都是全局唯一的**。也就是说，哪怕两个端口类型相同，它们的 `PortId` 也不同。这避免了「在同一作用域内两个同名实体」的歧义。

#### 4.1.2 核心流程

四类实体构成一个有向的交叉引用网，主方法名遵循一个简单约定——**复数方法返回多个实体**（如 `net_pins()` 返回该网的所有引脚），单数方法返回一个。

```
        +---------+      pin_block()
        |  Block  |<--------------------------+
        +---------+                           |
           |   ^                          +---------+  net_pins()  +---------+
           |   |                          |   Pin   |<-------------|   Net   |
block_ports |   | port_block()            |         |------------->|         |
           v   |                          +---------+  pin_net()   +---------+
        +---------+      port_pins()         ^   |
        |  Port  |-----------------------+   |   |
        +---------+                      pin_port()
```

（图源：netlist.h 顶部文档注释）

由此可推出两条最常用的遍历链路：

- **由 Net 找所有相关 Block**：`net_pins(net)` → 对每个 pin 调 `pin_block(pin)`（或先 `pin_port` 再 `port_block`）。
- **由 Block 找所有输入 Net**：`block_input_pins(blk)` → 对每个 pin 调 `pin_net(pin)`。

#### 4.1.3 源码精读

**四类实体的关系图**直接画在头文件注释里，是理解全类的钥匙：

[netlist.h:89-108](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L89-L108) —— `Netlist` 顶部用 ASCII 画出 Block/Port/Pin/Net 四者通过 `pin_block()`、`net_pins()`、`pin_net()`、`port_pins()` 等方法相互指认的关系。

**类的模板签名**揭示了「四元」是四个模板参数：

[netlist.h:440-441](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L440-L441) —— `template<typename BlockId = ParentBlockId, typename PortId = ParentPortId, typename PinId = ParentPinId, typename NetId = ParentNetId> class Netlist`。四个 ID 类型都带默认值（基类自用的 `Parent*Id`），子类可以换成自己的 `AtomBlockId`、`ClusterBlockId` 等。

**Pin 与 Net 的核心查询方法声明**：

[netlist.h:613](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L613) —— `NetId pin_net(const PinId pin_id) const;`，由引脚找网。

[netlist.h:654](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L654) —— `pin_range net_pins(const NetId net_id) const;`，由网找全部引脚（第一个是驱动，其余是接收）。

[netlist.h:663](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L663) —— `PinId net_driver(const NetId net_id) const;`，取网的驱动引脚（可能为无效）。

**创建实体的入口**（`protected`，供子类调用）：

[netlist.h:902-930](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L902-L930) —— `create_block` / `create_port` / `create_pin` / `create_net`。注意 `create_pin` 的参数顺序是 `(port_id, port_bit, net_id, pin_type)`，它一次就把「引脚—端口—网」三方关系同时建立起来。

**PinType 枚举**说明引脚只有两种有效角色（驱动或接收）加一个「悬空」态：

[netlist_fwd.h:42-46](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist_fwd.h#L42-L46) —— `enum class PinType { DRIVER, SINK, OPEN };`，`DRIVER` 驱动一根网，`SINK` 是网的接收端，`OPEN` 表示尚未决定。

#### 4.1.4 代码实践

**实践目标**：在不运行任何代码的前提下，纯靠阅读源码，亲手在纸上重建「四元实体 + 主存储」的对应表，并走通一条查询链。

**操作步骤**：

1. 打开 `vpr/src/base/netlist.h`，定位到类的 `private: //Data` 区段（约 1100 行起）。
2. 对每一类实体，找出「记录它是否有效的主 ID 容器」与「记录它属性/关系的容器」：
   - Block：`block_ids_`（主）、`block_names_`、`block_ports_`、`block_pins_`、`block_num_input_pins_` 等。
   - Port：`port_ids_`（主）、`port_names_`、`port_blocks_`、`port_pins_`、`port_widths_`、`port_types_`。
   - Pin：`pin_ids_`（主）、`pin_ports_`、`pin_port_bits_`、`pin_nets_`、`pin_net_indices_`。
   - Net：`net_ids_`（主）、`net_names_`、`net_pins_`。
3. 画一张四列表格，列名：实体、主 ID 容器、ID 类型、关键关系容器。

**需要观察的现象**：你会发现每类实体的属性都被拆成多个并列的 `vector_map`（结构数组），而不是塞进一个 `struct Block { ... }`。这正是后面 4.3 要讲的 SoA 布局。

**预期结果**：得到如下（节选）对照表：

| 实体 | 主 ID 容器 | ID 类型（基类默认） | 关系容器示例 |
| --- | --- | --- | --- |
| Block | `block_ids_` | `ParentBlockId` | `block_pins_`、`block_ports_` |
| Port | `port_ids_` | `ParentPortId` | `port_blocks_`、`port_pins_` |
| Pin | `pin_ids_` | `ParentPinId` | `pin_nets_`、`pin_ports_` |
| Net | `net_ids_` | `ParentNetId` | `net_pins_` |

> 待本地验证：以上容器名取自 netlist.h 的 `private: //Data` 段落，建议你亲自 `grep` 一次确认拼写与所属行。

#### 4.1.5 小练习与答案

**练习 1**：一根 Net 最多有几个驱动引脚？源码依据在哪？

**参考答案**：恰好 1 个（或 0 个，即「无驱动」）。依据 `associate_pin_with_net`：驱动引脚固定存在 `net_pins_[net_id][0]`（索引 0），且断言该位置原值必须为 `PinId::INVALID()`，即不允许已有驱动。见 [netlist.tpp:1946-1952](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.tpp#L1946-L1952)。

**练习 2**：`net_pins(net)` 返回的范围里，第一个元素一定是驱动吗？没有驱动时它是什么？

**参考答案**：是的，第一个元素（索引 0）是驱动；当网没有驱动时，`net_driver()` 返回 `PinId::INVALID()`。见 [netlist.tpp:395-403](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.tpp#L395-L403)。

---

### 4.2 StrongId 类型安全设计

#### 4.2.1 概念说明

四元模型的 ID 若用裸 `int`，就会出现 2.3 节那种「把块 ID 当网 ID 传」的隐患。VTR 用 `vtr::StrongId` 一招解决：它是一个模板，**第一个模板参数是一个只起「打标签」作用的空类型（tag）**。两个 `StrongId` 只要 tag 不同，编译器就视它们为不相关的类型，互相赋值/传参直接报错。

`StrongId` 的模板参数有三个：

1. **Tag**（必填）：唯一标识「这是哪一种 ID」的幽灵类型。
2. **T**（默认 `int`）：底层整数类型。
3. **sentinel**（默认 `-1`）：表示「无效 ID」的哨兵值。

关键接口：

- 默认构造出的 ID 就是哨兵值（无效）。
- `MyId::INVALID()` 返回一个无效 ID。
- `if (id)` / `id.is_valid()` 判断有效性。
- `size_t(id)` 显式转成下标，用于索引容器——**必须显式**，不会偷偷转换。
- 不同 tag 的 `StrongId` 之间**不可**隐式转换。

#### 4.2.2 核心流程

`StrongId` 的类型安全，本质是「让编译器替你检查 ID 种类」。可以把它想成一个「穿了制服的整数」：

```
StrongId<blk_tag>  →  「我是块 ID」   ←─┐
StrongId<net_tag>  →  「我是网 ID」     │  tag 不同 ⇒ 编译期视为不同类型
StrongId<pin_tag>  →  「我是引脚 ID」 ←─┘  混用 ⇒ 编译错误（而非运行时 bug）
```

底层数值仍是整数（默认 `int`），所以做下标时通过显式 `size_t(id)` 转换，性能与裸整数无异——类型安全是**零运行时开销**的。

`Netlist` 基类在 `netlist_fwd.h` 里用四个空结构体当 tag，定义出四个「父 ID」别名；子类（`AtomNetlist`/`ClusteredNetlist`）再各自派生自己的 ID 类（继承父 ID，复用同一个 tag）。这套继承让「父类方法接受 `ParentBlockId`、子类传 `AtomBlockId`」天然兼容，又保留了子类自带 `INVALID()` 与哈希特化的能力。

#### 4.2.3 源码精读

**StrongId 的「动机」注释本身就是最好的教材**，它对比了裸 int、typedef、StrongId 三种写法：

[vtr_strong_id.h:38-61](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_strong_id.h#L38-L61) —— 注释演示：`count_net_teriminals(blk_id)` 在 typedef 写法下静默通过（bug），在 `StrongId` 写法下编译报错（`NetId expected!`）。

**类模板定义**，三个模板参数与 `static_assert`：

[vtr_strong_id.h:175-177](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_strong_id.h#L175-L177) —— `template<typename tag, typename T = int, T sentinel = T(-1)> class StrongId`，并断言 `T` 必须是整型。

**有效性判断与下标转换**（注意都是 `explicit`，禁止隐式）：

[vtr_strong_id.h:194-203](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_strong_id.h#L194-L203) —— `explicit operator bool()`、`is_valid()`、`explicit operator std::size_t()`、`explicit operator int()`。`explicit` 是关键：它阻止了「ID 被悄悄当成 bool 或下标」的意外。

**Netlist 基类的四个 tag 与父 ID 别名**：

[netlist_fwd.h:12-27](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist_fwd.h#L12-L27) —— 定义 `general_blk_id_tag` 等 4 个空 tag，再 `typedef vtr::StrongId<...> ParentBlockId;`（以及 Port/Pin/Net）。注释里点明用 StrongId 是「to avoid type-conversion errors (e.g. passing a PinId where a NetId was expected)」。

**子类如何复用这套类型安全**（以 AtomNetlist 为例）：

[atom_netlist_fwd.h:30-36](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist_fwd.h#L30-L36) —— `class AtomBlockId : public ParentBlockId`，用 `using ParentBlockId::ParentBlockId;` 继承构造，并自带 `static constexpr AtomBlockId INVALID()`。同文件对 `AtomNetId`/`AtomPortId`/`AtomPinId` 做同样处理。`ClusteredNetlist` 的 `ClusterBlockId` 等结构完全平行（见 `clustered_netlist_fwd.h`）。

> 这也解释了为何 `Netlist` 模板要把 ID 类型做成参数：基类用 `Parent*Id`，`AtomNetlist` 传 `Atom*Id`，`ClusteredNetlist` 传 `Cluster*Id`——三套 ID 同源（继承自同一个父），所以同一份基类代码可以无缝服务三种网表。

#### 4.2.4 代码实践

**实践目标**：亲手验证 StrongId 的「编译期类型安全」确实生效。

**操作步骤**（源码阅读 + 思想实验）：

1. 阅读 [vtr_strong_id.h:79-90](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_strong_id.h#L79-L90) 中 Example 1~3，确认 tag、底层类型、sentinel 三个参数的含义。
2. 在脑中（或本地建一个最小 `.cpp`）写下面这段「示例代码」（**非项目原有代码**，仅用于验证概念）：

```cpp
// 示例代码：验证 StrongId 类型安全
struct my_blk_tag {};
struct my_net_tag {};
using MyBlkId = vtr::StrongId<my_blk_tag>;
using MyNetId = vtr::StrongId<my_net_tag>;

int count_terminals(MyNetId net_id);

void demo() {
    MyBlkId blk = 5;
    MyNetId net = 7;
    count_terminals(net);   // OK
    count_terminals(blk);   // 编译错误：MyNetId expected
}
```

3. 想清楚：如果把 `StrongId` 换成 `typedef int MyBlkId;`，第 11 行会发生什么？（答：静默编译通过，埋下 bug。）

**需要观察的现象**：`StrongId` 版本下，混用两种 ID 在编译阶段就被拦下；typedef 版本则一路放行。

**预期结果**：理解 StrongId 的类型安全是**编译期、零运行时开销**的，这正是它能用在性能敏感的 CAD 热路径上的原因。

> 待本地验证：上述示例代码未在仓库内运行，建议本地用 `g++ -c` 单独编译验证报错信息。

#### 4.2.5 小练习与答案

**练习 1**：`StrongId` 默认构造（无参）得到的值是什么？用什么判断它「无效」？

**参考答案**：默认构造得到 sentinel（默认 `-1`），即无效 ID。判断方式：`id == MyId::INVALID()`，或 `if (!id)`，或 `id.is_valid()`。见 [vtr_strong_id.h:181-197](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_strong_id.h#L181-L197)。

**练习 2**：为什么 `operator std::size_t()` 要标 `explicit`？去掉会怎样？

**参考答案**：为了禁止隐式转换。若不标 `explicit`，任何接受 `size_t` 的地方都能偷偷吃掉一个 ID，等于绕过了类型安全；标了之后，索引容器必须显式写 `vec[size_t(id)]`，意图清晰、不会被误用。见 [vtr_strong_id.h:199-200](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_strong_id.h#L199-L200)。

---

### 4.3 容器与查找接口：vector_map 与交叉引用

#### 4.3.1 概念说明

有了强类型 ID，还需要一种「用 ID 当下标」的容器。`vtr::vector_map<K, V>` 就是这样的容器：它本质是一个 `std::vector<V>`，但 `operator[]` 的下标类型是 `K`（某个 StrongId），而不是 `size_t`。

为什么不用 `std::map` 或 `std::unordered_map`？

- `std::map` 会同时存 key 和 value，还要维护红黑树，内存与缓存都不友好。
- VTR 的 ID 是**从 0 开始连续递增**的整数（创建时 `id = Id(container.size())`，依次追加），所以「ID 即下标」是最紧凑的存储：只存 value，`vec[id]` 直接定位，O(1) 且缓存连续。

`Netlist` 把这个容器用到极致——采用**结构数组（Struct-of-Arrays, SoA）**布局：不是「一个 `struct Block` 里装着名字、端口、引脚」，而是把所有块的名字放一个 `vector_map`、所有块的端口放另一个、所有块的引脚放第三个……这样遍历「所有块的名字」时，内存完全连续，缓存命中率高。

交叉引用就建立在这些容器之上：`pin_nets_`（每个 pin 对应哪个 net）、`net_pins_`（每个 net 含哪些 pin）等，构成双向指针（用 ID 表示）。

#### 4.3.2 核心流程

一次「创建引脚并连到网」的操作，会同时更新多个容器，维持交叉引用的一致性。以 `create_pin(port, bit, net, type)` 为例：

```
create_pin(port_id, port_bit, net_id, type)
  │
  ├─ 若 (port_id, port_bit) 已存在 → 返回已有 pin_id
  │
  └─ 否则新建 pin_id = pin_ids_.size()：
       1. pin_ids_.push_back(pin_id)            # 登记 pin 有效
       2. pin_ports_.push_back(port_id)         # pin → port
       3. pin_port_bits_.push_back(port_bit)    # pin 在 port 中的位
       4. pin_nets_.push_back(net_id)           # pin → net   ★ 由 pin 查 net 的依据
       5. pin_is_constant_.push_back(is_const)
       6. associate_pin_with_net(pin, type, net)# net → pin   ★ 由 net 查 pin 的依据
            · DRIVER ⇒ net_pins_[net][0] = pin
            · SINK   ⇒ net_pins_[net].emplace_back(pin)
       7. pin_net_indices_.push_back(返回的索引) # pin 在 net 中的位置
       8. associate_pin_with_port(...)          # port → pin
       9. associate_pin_with_block(...)         # block → pin
```

注意第 4 步和第 6 步是**双向**的：`pin_nets_` 让你「由 pin 查 net」，`net_pins_` 让你「由 net 查 pin」。这两条链是本讲实践任务的核心。

关于「脏标记 + 压缩」：`remove_*()` 不会立刻删除数据，只把对应 ID 标记为无效（`dirty_ = true`）。此时遍历可能遇到无效 ID。等所有删除完成后，调用一次 `compress()` 真正剔除无效项并重排，返回一个 `IdRemapper`（旧 ID → 新 ID）供客户端更新手头的 ID。这样把昂贵的重排分摊到一次，而非每次删除都重排。

#### 4.3.3 源码精读

**vector_map 的核心：以 StrongId 为下标**

[vtr_vector_map.h:73-86](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector_map.h#L73-L86) —— `const_reference operator[](const K n) const`，内部 `size_t index = size_t(n); ... return vec_[index];`。下标类型是泛型 `K`（StrongId），靠显式 `size_t()` 转换落到底层 vector。这正是 4.2 里 `operator size_t()` 的用武之地。

[vtr_vector_map.h:151-159](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector_map.h#L151-L159) —— `insert(key, value)`：若 key 超出当前容量，先 `resize` 并用 `Sentinel::INVALID()` 填补「空隙」，再写入。这就是「ID 不连续时会留洞」的来源（注释见 [vtr_vector_map.h:28-29](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector_map.h#L28-L29)）。

**Netlist 的结构数组布局**（private 数据成员）：

[netlist.h:1106-1155](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L1106-L1155) —— Block/Port/Pin/Net 各自的属性被拆成一堆并列的 `vector_map`，例如 Pin 段：`pin_ids_`、`pin_ports_`、`pin_port_bits_`、`pin_nets_`、`pin_net_indices_`、`pin_is_constant_`；Net 段：`net_ids_`、`net_names_`、`net_pins_`。这就是 SoA。

**「由 pin 查 net」的实现**——一行容器取值：

[netlist.tpp:309-313](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.tpp#L309-L313) —— `pin_net(pin_id)` 先 `VTR_ASSERT_SAFE(valid_pin_id(...))`，再 `return pin_nets_[pin_id];`。直接拿 pin 当下标，从 `pin_nets_` 取出对应的 net。

**「由 net 查 pins」与驱动引脚**：

[netlist.tpp:373-377](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.tpp#L373-L377) —— `net_pins(net_id)` 把 `net_pins_[net_id]` 这个 `std::vector<PinId>` 包装成一个可迭代的 `pin_range` 返回。

[netlist.tpp:395-403](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.tpp#L395-L403) —— `net_driver(net_id)` 返回 `net_pins_[net_id][0]`；若该网没有任何引脚则返回 `PinId::INVALID()`。这印证了「驱动恒在索引 0」的约定。

**双向交叉引用的建立**（create_pin 的核心）：

[netlist.tpp:732-777](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.tpp#L732-L777) —— `create_pin` 依次维护 `pin_ports_/pin_nets_/pin_is_constant_`，再调 `associate_pin_with_net` 写反向边，最后写 `pin_net_indices_`、关联 port 与 block。函数末尾一串 `VTR_ASSERT` 是「后置条件自检」，确保三方关系一致。

[netlist.tpp:1944-1960](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.tpp#L1944-L1960) —— `associate_pin_with_net`：DRIVER 写入 `net_pins_[net][0]` 并断言原槽为空（一网一驱动）；SINK 则 `emplace_back` 追加到末尾。返回该 pin 在 net 中的索引。

**NVI（非虚接口）约定**——基类与子类的分工：

[netlist.h:1071-1094](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L1071-L1094) —— 一组 `virtual ... *_impl()` 方法（`clean_blocks_impl`、`remove_block_impl`、`rebuild_*_refs_impl` 等）。基类的 `remove_*()`/`clean_*()` 是非虚的「外壳」，内部调用这些 `*_impl()`，由子类覆盖以处理子类特有的数据。这是 C++「非虚接口惯用法」，保证了基类能统一控制流程顺序。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：在源码中亲手走通「由一个 PinId 找到它所属 NetId」的完整链路，并写出伪代码。这是后续布局、布线阶段最频繁的操作之一。

**操作步骤**：

1. 打开 [netlist.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h)，确认 `pin_net` 的声明在第 613 行，参数是 `PinId`、返回 `NetId`。
2. 跳到 [netlist.tpp:309](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.tpp#L309)，确认实现体就是 `return pin_nets_[pin_id];`。
3. 回到 netlist.h 的数据段，确认 `pin_nets_` 的声明：

   [netlist.h:1134](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L1134) —— `vtr::vector_map<PinId, NetId> pin_nets_;`，注释「Net associated with each pin」。

4. 至此链路清晰：`PinId` 作为下标 → `pin_nets_` 这个 `vector_map<PinId,NetId>` → 取出 `NetId`。底层即 `vec_[size_t(pin_id)]`。

**需要观察的现象**：整个查询是 O(1) 的一次数组访问，且 `pin_nets_` 与 `net_pins_` 是互相反向的两张表（pin→net 与 net→pin），印证 4.3.2 的双向交叉引用。

**预期结果**：写出如下伪代码（注释说明每步对应的源码位置）：

```text
# 输入：netlist 中的某个 pin_id
# 目标：得到它所属的 net_id

function pin_to_net(pin_id):
    # 可选：先校验 pin_id 有效（valid_pin_id，见 netlist.h:716）
    assert netlist.valid_pin_id(pin_id)
    # 核心一步：以 pin_id 为下标，从 pin_nets_ 取 net_id
    # 对应 netlist.tpp:309-313 中的  return pin_nets_[pin_id]
    net_id = netlist.pin_nets_[pin_id]   # 即 netlist.pin_net(pin_id)
    return net_id
```

若要进一步「由这个 pin 一路找到所在 block 的名字」，链路是：

```text
pin_id
  → netlist.pin_port(pin_id)        # pin → port   (pin_ports_, netlist.tpp:323)
  → netlist.port_block(port_id)     # port → block (port_blocks_)
  → netlist.block_name(blk_id)      # block → 名字 (block_names_ → strings_)
```

或走捷径 `netlist.pin_block(pin_id)`（[netlist.tpp:337-343](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.tpp#L337-L343)，内部就是 `port_block(pin_port(pin))`），跳过显式的 port 中间步。

> 待本地验证：上述伪代码基于源码阅读推导，未实际编译运行；建议在阅读 u3-l2（AtomNetlist）后，回到本练习，用一个真实 `AtomNetlist` 对象打印验证。

#### 4.3.5 小练习与答案

**练习 1**：`vector_map` 与 `std::map` 相比，对 key 有什么额外要求？为什么？

**参考答案**：要求 key 能转成 `size_t` 且**连续递增**（线性）。因为底层是 vector，`vec_[size_t(key)]` 直接定位；若 key 稀疏不连续，会为中间的空洞分配空间（填 `Sentinel::INVALID()`），内存浪费。见 [vtr_vector_map.h:18-19](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector_map.h#L18-L19) 与 [vtr_vector_map.h:36-39](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector_map.h#L36-L39)。这正是 Netlist 强制「删除后要 compress 重排成连续」的原因。

**练习 2**：调用 `remove_block(blk)` 之后立刻遍历 `blocks()`，可能看到什么？怎样才能拿到「干净」的网表？

**参考答案**：可能看到无效（INVALID）的 BlockId，因为 `remove_*()` 只打标记、设 `dirty_=true`，并不真删（见 netlist.h 顶部「Netlist is NOT compressed ('dirty')」段，[netlist.h:315-324](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L315-L324)）。需要调用 `compress()`（或 `remove_and_compress()`）真正剔除无效项并重排为连续，此时会返回 `IdRemapper` 用于更新外部持有的旧 ID（见 [netlist.h:894](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L894)）。

**练习 3**：为什么 Netlist 把属性拆成「结构数组」（多个 `vector_map`）而不是「数组结构」（`vector<struct Block>`）？

**参考答案**：为了缓存局部性。绝大多数代码一次只访问某一种属性（如只遍历所有块的名字），SoA 让同类属性在内存中连续，减少无用数据被载入缓存；同时也便于按需扩缩某一种属性。见 netlist.h 顶部「Implementation Details」段，[netlist.h:326-337](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L326-L337)。

## 5. 综合实践

把本讲三个模块串起来，完成一个「**Netlist 数据侦探**」小任务：假设有人递给你一个已经构建好的 `Netlist` 对象（具体是 Atom 还是 Clustered 不重要，基类接口通用），请只用本讲学过的接口，回答三个问题，并标注每一步依据的源码行。

任务背景：网表里有一根名叫 `"clk"` 的网，你想知道**它由哪个块驱动**、**它驱动了哪些块**、以及**这些块的输入引脚里有多少是时钟引脚**。

参考解题思路（伪代码 + 源码依据）：

```text
# 1. 由名字找到 net_id（查表）
net_id = netlist.find_net("clk")
#    依据：netlist.h:781  NetId find_net(const std::string& name) const;

# 2. 找驱动块
driver_pin = netlist.net_driver(net_id)          # netlist.tpp:395  net_pins_[net][0]
driver_blk  = netlist.pin_block(driver_pin)      # netlist.tpp:337  port_block(pin_port(pin))
print("驱动块:", netlist.block_name(driver_blk)) # netlist.tpp:101  block_names_ → strings_

# 3. 遍历该网所有 pin，统计 sink 落在哪些块
for pin in netlist.net_pins(net_id):             # netlist.tpp:373  net_pins_[net]
    blk = netlist.pin_block(pin)
    if blk != driver_blk:
        print("被驱动块:", netlist.block_name(blk))

# 4. 统计某个被驱动块的时钟引脚数
clock_pins = netlist.block_clock_pins(blk)       # netlist.h:532  block_num_clock_pins_ 分段
print("时钟引脚数:", len(clock_pins))
```

完成后，请回答两个延伸问题（答案可在本讲源码中找到）：

1. `find_net("clk")` 若找不到，返回什么？（提示：[netlist.h:778-781](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L778-L781)）
2. 第 4 步 `block_clock_pins` 之所以能返回一个连续范围，依赖的是哪个数据成员与 4.1 图里的哪种「分段布局」？（提示：[netlist.h:1117](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L1117) 与头文件「Block pins/Block ports data layout」段 [netlist.h:350-381](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L350-L381)）

> 待本地验证：本综合实践为源码阅读型任务，伪代码基于公开接口推导；建议在学完 u3-l2（AtomNetlist）后，用真实网表对象实跑验证。

## 6. 本讲小结

- `Netlist` 用 **Block / Port / Pin / Net** 四类实体刻画网表，四者通过 `pin_net`/`net_pins`/`pin_block`/`port_pins` 等方法相互交叉引用；每根 Net 恰有 1 个驱动引脚（恒在 `net_pins_[net][0]`）和若干接收引脚。
- 所有 ID 都是 `vtr::StrongId`：靠**幽灵 tag** 在编译期区分种类，`explicit` 转换杜绝隐式误用，类型安全**零运行时开销**。
- 基类在 `netlist_fwd.h` 定义四个 `Parent*Id`（带 `general_*_tag`）；`AtomNetlist`/`ClusteredNetlist` 各自派生 `Atom*Id`/`Cluster*Id` 继承之，故同一份模板基类可服务三种网表。
- 主存储是 `vtr::vector_map<K,V>`——一个以 StrongId 为下标的 vector，要求 ID 连续递增；配合**结构数组（SoA）**布局，换取内存紧凑与缓存友好。
- 增删遵循「**脏标记 + 批量压缩**」：`remove_*()` 只标无效，`compress()` 一次性剔除重排并返回 `IdRemapper`；故 `is_dirty()` 期间遍历可能撞到无效 ID。
- 基类用 **NVI（非虚接口）** 约定与子类分工：`create_*()` 由子类调基类，`remove_*()`/`clean_*()`/`rebuild_*_refs()` 由基类调子类的 `*_impl()`。

## 7. 下一步学习建议

本讲只讲了**基类骨架**，下一步应当看「骨架上长出的肉」：

- **u3-l2 AtomNetlist 原子级网表**：看 `AtomNetlist` 如何在 `Netlist` 之上增加 `t_model`（原语模型）与真值表（TruthTable），以及如何从 BLIF 文件读入并填充四元结构。阅读 `vpr/src/base/atom_netlist.h` 与 `read_blif.h`。
- **u3-l3 ClusteredNetlist 聚簇网表**：看打包产物如何用 `ClusterBlockId` 等另一套 ID 表达逻辑块级网表，并携带物理类型 `t_logical_block_type` 与簇内层次 `t_pb`/`t_pb_route`。
- **u3-l4 VprContext 与全局状态**：看 `AtomContext`/`ClusteringContext` 如何把 `AtomNetlist`/`ClusteredNetlist` 作为成员挂进全局 `g_vpr_ctx`，理解本讲的网表在整个流程里「住在哪里」。

延伸阅读：想更深理解容器的，可对比 `libs/libvtrutil/src/` 下的 `vtr_vector.h`、`vtr_linear_map.h`、`vtr_range.h`，弄清 `vector` / `vector_map` / `linear_map` 三者的取舍。
