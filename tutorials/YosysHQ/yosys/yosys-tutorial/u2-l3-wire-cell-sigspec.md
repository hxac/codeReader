# Wire、Cell 与 SigSpec 初识

## 1. 本讲目标

上一讲我们打开了 `RTLIL::Design` 与 `RTLIL::Module` 这两个“容器”，知道了设计在内存里是一棵 `Design → Module` 的树。但模块内部到底装了什么？本讲就钻进 `Module` 内部，认识构成网表的三大基本零件：

- **Wire（线网）**：一根带位宽的连线，对应 Verilog 里的 `wire` / `reg` / 端口。
- **Cell（单元）**：一个被实例化的“器件”，对应 Verilog 里的逻辑门、触发器，或一个子模块实例。
- **SigSpec（信号说明）**：用来描述“一段信号”的通用语言——它可以是一根完整的线、一根线的一段切片、一个常数，甚至它们的拼接。

学完本讲你应当能够：

1. 说清楚 Wire 与 Cell 各自代表什么、有哪些关键字段。
2. 理解 Cell 不是用“引脚连线”而是用“端口名 → SigSpec”的字典来表达连接关系。
3. 读懂 SigSpec 的“块（chunk）/ 位（bit）”两层结构，知道一段信号为什么能同时表示线、切片和常数。
4. 对一个综合后的设计执行 `write_rtlil`，在输出文本里指认某个 `$` 单元（如 `$and`、`$dff`）的端口分别连到了哪些 wire。

## 2. 前置知识

本讲假设你已经读过 **u2-l1（RTLIL 文本格式）** 和 **u2-l2（Design 与 Module）**。我们这里简单回顾两个关键点：

- **RTLIL 的命名约定**：所有标识符都以 `\` 或 `$` 开头。`\name` 是“公有”名字，来自 HDL 源码（如 `\clk`、`\count`）；`$name` 是 Yosys 自动生成的内部名字（如 `$0`、`\$and` 这种单元类型）。两者前缀不同，永远不会冲突。详见 [kernel/rtlil.h:33-40](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L33-L40) 旁边的 `State` 枚举所在的同一组基础定义。
- **位状态**：RTLIL 的每一位不只是 0/1，而是一个 `RTLIL::State` 枚举，共 6 种取值。这一点对理解 SigSpec 很重要，所以先看它的定义。

> 名字与状态的小词典

| 术语 | 含义 |
|---|---|
| `State::S0 / S1` | 确定的逻辑 0 / 1 |
| `State::Sx` | 不确定值（undefined），也用于冲突 |
| `State::Sz` | 高阻（high-impedance / 未连接） |
| `State::Sa` | 无关项（don't care），仅用于 case 匹配 |
| `State::Sm` | 标记位，部分 pass 内部使用 |

只要记住“一位可以是 0/1/x/z”，就足够理解本讲了。

## 3. 本讲源码地图

本讲几乎全部围绕 Yosys 的核心头文件与实现文件展开：

| 文件 | 作用 |
|---|---|
| [kernel/rtlil.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h) | RTLIL 所有数据结构的声明：`SigChunk`、`SigBit`、`SigSpec`、`Wire`、`Cell`、`Module` 全在这里。 |
| [kernel/rtlil.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc) | 上述结构的成员函数实现，以及 `Module` 提供的 `addWire` / `addCell` / `connect` 等构造接口。 |
| [kernel/rtlil_bufnorm.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil_bufnorm.cc) | `Cell::setPort` / `unsetPort` 的实现放在这里（与“缓冲归一化”优化相关）。 |
| [backends/rtlil/rtlil_backend.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc) | `write_rtlil` 后端：把内存里的 Wire/Cell/SigSpec 拍平成文本，是本讲实践的“对照表”。 |
| [examples/cmos/counter.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/counter.v) | 一个 3 位计数器，作为本讲动手实践的小设计。 |

一个心智模型：在 `Module` 内部，**Wire 是节点，Cell 也是节点，而 SigSpec 是把它们“连起来”的胶水**。Cell 不直接持有 Wire 指针，而是通过“端口名 → SigSpec”来表达“我的 A 引脚接到了这段信号上”。

---

## 4. 核心概念与源码讲解

### 4.1 RTLIL::Wire：一根带属性的连线

#### 4.1.1 概念说明

`RTLIL::Wire` 表示模块里的一根线网。它可以对应 Verilog 源码里的：

- `input clk;` / `output reg [2:0] count;` —— 端口线；
- `wire [7:0] data;` —— 普通连线；
- `reg [31:0] state;` —— 由 always 驱动的寄存器（在 RTLIL 里，寄存器也只是一根 Wire，时序信息体现在驱动它的 `$dff` 单元上）。

一句话：**Wire 只描述“这根线叫什么、有多宽、是不是端口”，不描述“谁驱动它、值是多少”**——驱动关系由 Cell 表达。

它继承自 `RTLIL::NamedObject`，因此“既有名字（`name`），又有一张属性表（`attributes`）”。继承链见 [kernel/rtlil.h:1261-1299](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1261-L1299)：

```cpp
struct RTLIL::AttrObject {
    dict<RTLIL::IdString, RTLIL::Const> attributes;   // 通用属性表
    ...
};
struct RTLIL::NamedObject : public RTLIL::AttrObject {
    RTLIL::IdString name;                              // 对象名字
};
```

#### 4.1.2 核心流程

一根 Wire 的生命史很短：

1. `Module::addWire(name, width)` 用 `new` 创建一个 Wire，填入名字与位宽，再交给 `Module::add()` 登记到模块的 `wires_` 字典里（同时设置反向指针 `wire->module`）。
2. 如果它是端口，前端会再设 `port_input` / `port_output` / `port_id`。
3. 综合过程中，各种 pass 可能给 Wire 打属性（如 `src` 源码定位、`init` 初值）。
4. 当某个 Cell 通过 `setPort` 把这根线设为输出端口时，Wire 会缓存“谁在驱动我”（`driverCell_`），方便快速查询驱动者。

需要特别理解的字段是 **`upto`** 与 **`start_offset`**：Verilog 允许 `wire [0:7] x;`（升序）或 `wire [4:0] y;`（带偏移）。RTLIL 统一把“第 0 位”规定为 LSB，并用 `upto` 标记原始方向是否反过来、用 `start_offset` 记录原始起点偏移。

#### 4.1.3 源码精读

Wire 的结构定义在 [kernel/rtlil.h:2431-2479](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2431-L2479)，关键字段如下（这段代码声明了 Wire 的全部公开状态）：

```cpp
struct RTLIL::Wire : public RTLIL::NamedObject {
    ...
    RTLIL::Module *module;                              // 反向指针：我属于哪个模块
    int width, start_offset, port_id;                   // 位宽、起点偏移、端口序号
    bool port_input, port_output, upto, is_signed;      // 端口方向、升序、是否有符号
    ...
};
```

- `width` 是核心：它就是这根线的位数。Yosys 还提供了一个全局便捷函数 `GetSize(wire)` 直接返回 `width`（[kernel/rtlil.h:2481-2483](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2481-L2483)）。
- `port_input` / `port_output` 两个 bool 共同描述端口方向（同时为真即 `inout`）。
- `upto` 配合 `from_hdl_index` / `to_hdl_index` 完成“HDL 下标 ↔ RTLIL 下标”的换算。

构造函数的默认值见 [kernel/rtlil.cc:4236-4254](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4236-L4254)，它把 width 默认设为 1、所有 bool 默认为 false：

```cpp
RTLIL::Wire::Wire() {
    ...
    module = nullptr;
    width = 1;
    start_offset = 0;
    port_id = 0;
    port_input = false;
    port_output = false;
    upto = false;
    is_signed = false;
}
```

而真正“诞生一根 Wire”的入口是 `Module::addWire`，见 [kernel/rtlil.cc:3173-3181](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3173-L3181)：

```cpp
RTLIL::Wire *RTLIL::Module::addWire(RTLIL::IdString name, int width) {
    log_assert(width >= 0 && width < RTLIL::WIDTH_LIMIT);
    RTLIL::Wire *wire = new RTLIL::Wire;
    wire->name = std::move(name);
    wire->width = width;
    add(wire);            // 登记进 wires_，并设置 wire->module = this
    return wire;
}
```

#### 4.1.4 代码实践（源码阅读型）

打开 [kernel/rtlil.h:2431-2479](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2431-L2479)，做下面这件事：

1. 找到 `width`、`port_input`、`upto` 三个字段的声明行。
2. 阅读 `from_hdl_index` 方法，回答：若一根线 `upto = true`、`width = 4`、`start_offset = 0`，那么 HDL 里写 `x[3]`（在升序声明 `[0:3]` 中是最高位）会映射到 RTLIL 的第几位？

**预期结果**：`from_hdl_index` 里的 `rtlil_index = width - 1 - zero_index`，代入得 `4 - 1 - 3 = 0`，即 RTLIL 的第 0 位（LSB）。这印证了“RTLIL 永远以低位为索引 0”。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Wire 的拷贝构造被 `= delete` 禁止（见源码 `Wire(RTLIL::Wire &other) = delete;`）？

> **参考答案**：因为 Wire 的所有权归属于 `Module` 的 `wires_` 字典，且持有反向指针 `module`、缓存了 `driverCell_` 等交叉引用。若允许随意拷贝，会产生两个对象指向同一模块、同一驱动者却各自独立的混乱状态。Yosys 要求只能通过 `addWire` 创建、`remove` 销毁。

**练习 2**：一根 `inout` 端口线，`port_input` 和 `port_output` 分别是什么值？

> **参考答案**：两者都为 `true`。这与文本后端的 `inout` 输出对应（见 [backends/rtlil/rtlil_backend.cc:149-154](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L149-L154)）。

---

### 4.2 RTLIL::Cell：一个被实例化的器件

#### 4.2.1 概念说明

`RTLIL::Cell` 表示模块里**一个被实例化的器件**。它可以是：

- 一个 Yosys 内部基本单元，类型以 `$` 开头，如 `$and`（与门）、`$or`、`$not`、`$mux`（多路选择器）、`$dff`（D 触发器）、`$adff`（带异步复位的 D 触发器）、`$add`（加法器）等。
- 一个用户定义的子模块实例（此时 `type` 是 `\子模块名`）。
- 一个黑盒单元（如从 liberty 库读进来的具体工艺单元）。

和 Wire 一样，Cell 也继承自 `NamedObject`，所以它有名字（实例名 `name`）、有类型（`type`，即“它是什么器件”）、还有一张属性表。**关键区别**在于：Cell 还多了两张字典——**端口连接表 `connections_`** 和 **参数表 `parameters`**。

#### 4.2.2 核心流程

Cell 表达连接的方式很关键，请记住这个模型：

```
Cell（例：一个 $and 门，实例名 $0）
├── type  = "$and"              // 它是什么
├── parameters = { WIDTH: 3 }   // 它的参数（位宽等）
└── connections_ = {            // 它的每个端口接到了哪段信号（SigSpec）
        \A -> SigSpec(...),     // 输入 A
        \B -> SigSpec(...),     // 输入 B
        \Y -> SigSpec(...),     // 输出 Y
    }
```

也就是说，**Cell 不持有“Wire 指针”，而是持有“端口名 → SigSpec”的映射**。SigSpec 才指向具体的 Wire（或常数）。这种间接带来两个好处：

1. 一个端口可以接到“半根线 + 一个常数”的拼接，而不仅是单根线；
2. 断开/重连端口只需改这个字典，不必动 Wire 本身。

常见 `$` 单元的端口名是固定的（这是 Yosys 内部单元库的约定）：

| 单元类型 | 端口 | 含义 |
|---|---|---|
| `$and` / `$or` / `$xor` / `$add` | `A`, `B`, `Y` | 双输入、单输出 |
| `$not` / `$neg` | `A`, `Y` | 单输入、单输出 |
| `$mux` | `A`, `B`, `S`, `Y` | 二选一：S=0 选 A，S=1 选 B |
| `$dff` | `CLK`, `D`, `Q` | 边沿触发器 |
| `$adff` | `CLK`, `ARST`, `D`, `Q` | 带异步复位的触发器 |

#### 4.2.3 源码精读

Cell 的结构定义在 [kernel/rtlil.h:2501-2562](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2501-L2562)。最核心的是这三行（声明了 Cell 的“身份”与“连接”）：

```cpp
struct RTLIL::Cell : public RTLIL::NamedObject {
    ...
    RTLIL::Module *module;                                   // 反向指针
    RTLIL::IdString type;                                    // 单元类型，如 $and
    dict<RTLIL::IdString, RTLIL::SigSpec> connections_;      // 端口名 -> 信号
    dict<RTLIL::IdString, RTLIL::Const> parameters;          // 参数名 -> 常数
    ...
};
```

紧接着是一组端口访问接口（[kernel/rtlil.h:2522-2527](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2522-L2527)）：

```cpp
bool hasPort(RTLIL::IdString portname) const;
void unsetPort(RTLIL::IdString portname);
void setPort(RTLIL::IdString portname, RTLIL::SigSpec signal);
const RTLIL::SigSpec &getPort(RTLIL::IdString portname) const;
const dict<RTLIL::IdString, RTLIL::SigSpec> &connections() const;
```

它们的实现非常薄。`getPort` 就是查字典（[kernel/rtlil.cc:4356-4359](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4356-L4359)）：

```cpp
const RTLIL::SigSpec &RTLIL::Cell::getPort(RTLIL::IdString portname) const {
    return connections_.at(portname);
}
```

`setPort` 的实现位于 [kernel/rtlil_bufnorm.cc:589](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil_bufnorm.cc#L589)，核心是把信号写进 `connections_` 字典，并通知 monitor（监听器）：

```cpp
void RTLIL::Cell::setPort(RTLIL::IdString portname, RTLIL::SigSpec signal) {
    auto r = connections_.insert(portname);
    auto conn_it = r.first;
    if (!r.second && conn_it->second == signal) return;   // 没变化就跳过
    ...                                                    // 通知 module/design 的 monitors
}
```

创建一个 Cell 的入口是 `Module::addCell`，见 [kernel/rtlil.cc:3197-3204](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3197-L3204)：

```cpp
RTLIL::Cell *RTLIL::Module::addCell(RTLIL::IdString name, RTLIL::IdString type) {
    RTLIL::Cell *cell = new RTLIL::Cell;
    cell->name = std::move(name);
    cell->type = type;
    add(cell);
    return cell;
}
```

> 小贴士：Yosys 还为常用单元提供了更顺手的工厂方法，例如 `Module::addAnd(name, sig_a, sig_b, sig_y)` 内部就是 `addCell(name, "$and")` 后连续 `setPort(A/B/Y)`，并在末尾 `setParam` 设位宽。可以在 [kernel/rtlil.cc:3564-3573](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3564-L3573) 附近看到同模式的 `addConcat` 实现，双输入门（A/B/Y）的批量宏则集中在 [kernel/rtlil.cc:3346-3348](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3346-L3348)。

#### 4.2.4 代码实践（源码阅读型）

在 [kernel/rtlil.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc) 中搜索 `RTLIL::Module::addDff`（约 L3680 附近，依据上文 `$dff` 的端口 `CLK/D/Q`）。

1. 阅读它的实现，列出它依次调用了哪些 `setPort`。
2. 对比 `$dff`（端口 `CLK, D, Q`）与 `$adff`（多一个 `ARST`），体会“参数与端口共同定义单元语义”。

**预期结果**：`addDff` 会调用 `setPort(ID::CLK, ...)`、`setPort(ID::D, ...)`、`setPort(ID::Q, ...)`，并设置 `WIDTH` 参数。这与上表完全一致。若找不到精确行号，标注「待确认」并先用 `help` 文本对照即可。

#### 4.2.5 小练习与答案

**练习 1**：一个 Cell 的 `name` 和 `type` 有什么区别？分别举一个例子。

> **参考答案**：`name` 是这个**实例**的名字（“这个具体的门叫什么”），如 `\$0`；`type` 是它**属于哪种器件**（“它是什么”），如 `$and`。一个模块里可以有多个 `type == "$and"` 但 `name` 各不相同的实例。

**练习 2**：`Cell::input(portname)` 是怎么判断某端口是不是输入端口的？提示见 [kernel/rtlil.cc:4375-4385](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4375-L4385)。

> **参考答案**：若 `type` 是 Yosys 内部已知单元，则查内置单元表 `yosys_celltypes.cell_input(type, portname)`；否则找到 `type` 对应的模块定义，看该端口线的 `port_input` 标志。

---

### 4.3 RTLIL::SigSpec：描述“一段信号”的通用语言

#### 4.3.1 概念说明

如果说 Wire 是“节点”、Cell 是“器件”，那么 **SigSpec 就是描述“一段信号”的通用语言**。它的能力远超一根 Wire：

- 它可以是一根**完整的线**：`SigSpec(wire)`；
- 可以是一根线的**一段切片**：`SigSpec(wire, offset, width)`，例如 `count[1:0]`；
- 可以是一个**常数**：`SigSpec(Const("3'001"))`，例如复位值 `3'd0`；
- 可以是上面这些的**拼接**：`{ wire, 2'b00, wire[5:3] }`。

**正因如此，Cell 的端口才用 SigSpec 而不是 Wire\* 来表达连接**——一个端口完全可以接到“一根线的某几位 + 几位常数”的混合体上。

SigSpec 内部由更小的两个零件组成（先认识它们再看 SigSpec 就很自然）：

- **SigChunk（块）**：要么是“一根 Wire 的一段切片”（`wire + offset + width`），要么是“一个常数位向量”（`data`，仅当 `wire == NULL`）。定义见 [kernel/rtlil.h:1301-1326](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1301-L1326)。
- **SigBit（位）**：单个比特。当它落在某根线上时记录 `(wire, offset)`；当它是常数时记录 `(State data)`。两者共用一个 union，见 [kernel/rtlil.h:1328-1354](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1328-L1354)。

于是有层级关系：**SigSpec = 一串 SigChunk = 一串 SigBit（展开后）**。

#### 4.3.2 核心流程

SigSpec 最值得理解的设计是它的**双重内部表示**。看 [kernel/rtlil.h:1446-1451](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1446-L1451)：

```cpp
Representation rep_;        // CHUNK 或 BITS 二选一
AtomicHash hash_;           // 缓存的哈希值
union {
    RTLIL::SigChunk chunk_;              // 情况一：单个块
    std::vector<RTLIL::SigBit> bits_;    // 情况二：逐位数组（LSB 在 index 0）
};
```

用一个标签 `rep_` 区分两种形态：

- **CHUNK 形态**：整段信号恰好是“一个块”——比如就是一根完整的线，或一个常数。这是最常见的情况，用一个 `SigChunk` 就够，省内存。
- **BITS 形态**：信号是任意拼接，无法用一个块表达，就退化成“逐位数组”。

这是典型的“快慢路径优化”：简单情况走紧凑表示，复杂情况走通用表示。需要时两者会通过 `unpack()` 互相转换。

> 位序约定与一个常数的数值

RTLIL 规定 **LSB（最低位）在下标 0**。一段全常数 SigSpec 的数值就是把每一位按权求和。设第 \(i\) 位为 \(b_i\)（\(i=0\) 为 LSB），位宽为 \(n\)，则其无符号数值为

\[
\text{value} = \sum_{i=0}^{n-1} b_i \cdot 2^{i}
\]

例如 `3'001`（位串从高位写到低位，所以是 `b2=0, b1=0, b0=1`）的值就是 \(1\)。

SigSpec 还提供大量“判断这段信号是什么”的便捷方法，集中声明在 [kernel/rtlil.h:1695-1706](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1695-L1706)：

| 方法 | 返回真当且仅当 |
|---|---|
| `is_wire()` | 恰好是“一整根完整的线” |
| `is_chunk()` | 恰好只有一个块 |
| `is_fully_const()` | 全部位都是常数 |
| `is_fully_zero()` / `is_fully_ones()` | 全 0 / 全 1 |
| `is_fully_def()` / `is_fully_undef()` | 全确定（无 x/z） / 全不确定 |

#### 4.3.3 源码精读

SigSpec 的完整声明横跨 [kernel/rtlil.h:1406-1775](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1406-L1775)。几个最常用方法的实现如下。

**从 Wire 构造**（[kernel/rtlil.cc:4671-4680](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4671-L4680)）：位宽非 0 时直接用 CHUNK 形态包装一个 `SigChunk(wire)`，这正是“一根完整的线”这一最常见情形：

```cpp
RTLIL::SigSpec::SigSpec(RTLIL::Wire *wire) {
    if (wire->width != 0) {
        rep_ = CHUNK;
        new (&chunk_) RTLIL::SigChunk(wire);
    } else {
        init_empty_bits();
    }
    check();
}
```

**`is_wire()` 判定**（[kernel/rtlil.cc:5481-5489](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L5481-L5489)）：只有“唯一一个块、且该块就是一整根线”才算：

```cpp
bool RTLIL::SigSpec::is_wire() const {
    Chunks cs = chunks();
    auto it = cs.begin();
    if (it == cs.end()) return false;
    const RTLIL::SigChunk &chunk = *it;
    return chunk.wire && chunk.wire->width == size() && ++it == cs.end();
}
```

**`append` 拼接**（[kernel/rtlil.cc:5336-5363](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L5336-L5363)）：若当前是 CHUNK 且新追加的也是同根线的相邻段，就尽量合并（快路径）；否则退化为 BITS 逐位追加（慢路径）。

> SigSpec 与文本输出的对应

`write_rtlil` 后端如何把 SigSpec 打印成文本？见 [backends/rtlil/rtlil_backend.cc:120-133](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L120-L133)：单个块就直接打印；多个块就用 `{ 块1 块2 }` 包起来。而单个块怎么打印，见 `dump_sigchunk`（[backends/rtlil/rtlil_backend.cc:106-118](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L106-L118)）：

```cpp
void RTLIL_BACKEND::dump_sigchunk(std::ostream &f, const RTLIL::SigChunk &chunk, bool autoint) {
    if (chunk.wire == NULL) {
        dump_const(f, chunk.data, chunk.width, chunk.offset, autoint);   // 常数：3'001
    } else {
        if (chunk.width == chunk.wire->width && chunk.offset == 0)
            f << chunk.wire->name;                            // 整根线：\count
        else if (chunk.width == 1)
            f << chunk.wire->name << " [" << chunk.offset << "]";   // 单位：\count [1]
        else
            f << chunk.wire->name << " [" << (chunk.offset+chunk.width-1) << ":" << chunk.offset << "]"; // 切片
    }
}
```

这段代码直接决定了你在 `write_rtlil` 输出里会看到 `\count`、`\count [2:0]`、`3'001` 这样的写法。

#### 4.3.4 代码实践（源码阅读型）

打开 [kernel/rtlil.h:1475-1506](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1475-L1506)，这里有 SigSpec 的一长串构造函数重载。

1. 数一数它有多少种“构造方式”（从 `Const`、`Wire*`、`Wire*,offset,width`、`string`、`int`、`SigBit`、`vector<SigChunk>`、`vector<SigBit>` …）。
2. 思考：为什么 Yosys 要提供这么多重载？答案在于 SigSpec 是“万能信号表示”，凡是能被当成“一段信号”的东西，都应该能直接隐式转成 SigSpec，省去手动包装。

**预期结果**：你会发现大约十几种构造重载。它们让 `setPort(ID::A, someWire)`、`setPort(ID::A, Const(0))`、`setPort(ID::A, sigbit)` 都能直接编译通过——这是 RTLIL API 用起来简洁的关键。

#### 4.3.5 小练习与答案

**练习 1**：表达式 `{ \a, \b[1:0], 2'b00 }` 在 RTLIL 内存里大致长什么样？

> **参考答案**：它是一个 BITS 形态（或多块）的 SigSpec，由三个 chunk 拼成：`chunk(\a 整根)`、`chunk(\b 的第 0~1 位)`、`chunk(常数 2'b00)`。由于三段来源不同，无法合并成单个块，故会以多个 chunk（或退化为逐位数组）表示。

**练习 2**：`is_wire()` 与 `is_chunk()` 哪个条件更宽？举一个 `is_chunk() == true` 但 `is_wire() == false` 的例子。

> **参考答案**：`is_chunk()` 更宽——它只要求“只有一个块”。`count[1:0]`（一根线的切片）是单个块但不是整根线，所以 `is_chunk() == true` 而 `is_wire() == false`。整根线 `\count` 则两者都为真。

---

## 5. 综合实践：在 `write_rtlil` 输出里指认一个 `$` 单元的端口

本实践把 Wire / Cell / SigSpec 三个概念串起来，目标是：**亲眼看到综合后的网表，并解释某个 `$` 单元的每个端口连到了什么**。

### 5.1 实践目标

把 `examples/cmos/counter.v`（一个带同步复位的 3 位计数器）综合成 RTLIL 文本，在输出里找到至少一个 `$` 单元（例如触发器 `$dff`，或逻辑门 `$and` / `$mux` / `$add`），指出它的 `type`、`name`、参数，以及端口 A/B/Y（或 CLK/D/Q）分别连到哪些 wire 或常数。

### 5.2 操作步骤

在仓库根目录新建一个最小脚本 `count_rtlil.ys`（这是本实践的示例脚本，**不是**仓库自带文件）：

```yosys
# 示例脚本：把 counter 综合成 RTLIL 文本
read_verilog examples/cmos/counter.v
synth                         # 把行为级 always 翻译成 $ 单元
write_rtlil count.rtlil       # 输出 RTLIL 文本
```

> 注意：**不要**运行 `examples/cmos/counter.ys` 里的 `dfflibmap` / `abc -liberty`。那两步会把 `$` 单元映射成 liberty 库里的具名工艺单元（如 `DFFPORB` 之类），你就看不到 `$and` / `$dff` 了。要看“原始内部单元”，停在 `synth` 之后即可。

然后运行：

```bash
./build/yosys count_rtlil.ys
# 若尚未构建，请先按 u1-l2 完成 cmake -B build && cmake --build build
```

### 5.3 需要观察的现象

打开生成的 `count.rtlil`，你会看到类似下面的结构（节选示意，**具体单元名与位宽以本地输出为准**）：

```yosys
module \counter
  wire input 1 \clk
  wire input 2 \rst
  ...
  wire width 3 \count
  ...
  cell $dff $0               # <- 一个 D 触发器，实例名 $0
    parameter WIDTH 3
    parameter \CLK_POLARITY 1
    connect \CLK \clk        # CLK 端口 -> 整根线 \clk
    connect \D { \count ... }# D  端口 -> 一段拼接信号（多 chunk，用 { } 包裹）
    connect \Q \count        # Q  端口 -> 整根线 \count
  end
  ...
  cell $and $1               # <- 一个与门
    parameter WIDTH 3
    parameter A_SIGNED 0
    connect \A ...
    connect \B ...
    connect \Y ...
  end
end
```

请对照 [backends/rtlil/rtlil_backend.cc:173-192](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L173-L192) 的 `dump_cell` 阅读这段文本：每行 `connect \端口 信号` 就是 `Cell::connections_` 字典里的一条，其右值就是本讲学的 `SigSpec`——它可能是 `\clk`（`is_wire()==true`）、`{ ... }`（多块拼接）或 `3'001`（常数）。

### 5.4 预期结果（交付清单）

在你的笔记里填完这张表（以你实际找到的一个 `$` 单元为例）：

| 项 | 我的观察 |
|---|---|
| 单元 `type` | 例：`$dff` |
| 单元实例 `name` | 例：`$0` |
| 参数 | 例：`WIDTH=3` |
| 端口 1（名 → 连到） | 例：`\CLK → \clk`（一根完整线） |
| 端口 2（名 → 连到） | 例：`\Q → \count`（一根完整线） |
| 端口 3（名 → 连到） | 例：`\D → { ... }`（多块拼接的 SigSpec） |

能正确填出这张表，就说明你已把 Wire（`\clk`、`\count`）、Cell（`cell $dff $0`）、SigSpec（`\clk` 或 `{ ... }`）三者的关系彻底打通。

> 如果本地无法构建运行：上面的输出是“待本地验证”的示意，你可以改为纯阅读型实践——直接阅读 [examples/cmos/counter.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/counter.v)，预测 `count` 寄存器会被综合成一个 `$dff`（端口 CLK/D/Q），其 D 输入端会接到一段包含复位值 `3'd0` 与 `count+1` 的选择逻辑（`$mux`/`$add`）拼接信号上，然后对照同学或 CI 的实际输出验证。

## 6. 本讲小结

- **Wire** 是“一根带位宽、可带端口属性的线”，只描述几何（`width` / `start_offset` / `upto`）与方向（`port_input` / `port_output`），不描述驱动关系。
- **Cell** 是“一个被实例化的器件”，靠 `type` 说明种类、靠 `connections_`（端口名 → SigSpec 的字典）说明每个端口接到哪、靠 `parameters` 携带位宽等参数。
- **SigSpec** 是“一段信号”的通用表示，能同时涵盖整根线、线切片、常数以及它们的拼接；内部用 CHUNK / BITS 双重表示做“快慢路径”优化。
- 层级关系是：`Module` 拥有若干 `Wire` 与 `Cell`；`Cell` 不直接持有 `Wire`，而是通过 `SigSpec` 间接引用；`SigSpec` 由 `SigChunk` 组成，`SigChunk` 又可展开成 `SigBit`。
- `write_rtlil` 文本里的 `wire` / `cell ... end` / `connect \端口 信号` 就是这三者的一一对应映射，源码在 [backends/rtlil/rtlil_backend.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc)。
- 命名上，公有标识符以 `\` 开头、自动生成的内部名以 `$` 开头；位状态有 0/1/x/z 等共 6 种，常数写作 `宽度'位串`。

## 7. 下一步学习建议

到这里，你已经掌握了 RTLIL 网表层（Wire/Cell/SigSpec）的最小心智模型。接下来有两个方向：

1. **向“编程接口”深入**：本讲只读了字段定义。如果你想自己写 pass 去构造 / 修改网表，下一站是 **u3-l1（Module/Cell/Wire 的完整接口）**，它会系统讲 `addWire` / `addCell` / `connect` / `setPort` / `fixup_parameters` 等构造与维护 API，以及属性（attributes）系统的用法。
2. **向“信号分析工具”深入**：如果你更关心“怎么在一个已有网表上做信号查找、替换、归一化”，可以跳读 **u3-l2（SigSpec / SigBit / SigChunk 与 sigtools）**，认识 `SigMap` 如何解决同一信号的多重驱动表示问题——那是后续所有优化 pass（opt、techmap）的基础工具。

建议按 u3-l1 → u3-l2 的顺序阅读，为进入 u4（Pass 系统）和 u6（核心综合流程）打好数据结构底子。
