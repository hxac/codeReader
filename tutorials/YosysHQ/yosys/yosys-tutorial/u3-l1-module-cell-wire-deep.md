# Module / Cell / Wire 的完整接口

> 本讲属于「RTLIL 核心数据结构深入」单元（u3）的第一讲，承接 [u2-l3 Wire、Cell 与 SigSpec 初识](u2-l3-wire-cell-sigspec.md)。
> 上一讲我们建立了网表的最小心智模型：Module 拥有 Wire 与 Cell，Cell 通过 SigSpec 间接引用 Wire。
> 本讲要回答的问题是：**如果我正在写一个 Pass，想用 C++ 代码在内存里“凭空造出”一根线、一个门，并把它们连起来，应该调用哪些函数？这些函数内部又做了什么？**

## 1. 本讲目标

学完本讲后，你应该能够：

1. 用 `module->addWire()` / `module->addCell()` / `module->connect()` 在内存里构造出合法的 RTLIL 网表片段。
2. 理解 Cell 的两大核心存储——`connections_`（端口连接）与 `parameters`（参数），并能用 `setPort` / `getPort` / `setParam` / `getParam` 正确读写它们。
3. 理解「module 级 connect」与「cell 级 setPort」是两套不同的连接机制，不会混淆。
4. 了解属性系统 `AttrObject`，会用 `set_bool_attribute` 等接口给 wire/cell/module 打标记。
5. 能对照真实源码，列出「创建一个 `$and` 门并连接端口」所需的全部调用序列——这也是本讲的代码实践任务。

## 2. 前置知识

在动手之前，请确认你已经具备 u2 系列建立的认知。这里做一次最小回顾，并补两个本讲才需要的新概念。

### 2.1 已经知道的事（来自 u2）

- `RTLIL::Design` 按名字拥有若干 `RTLIL::Module`；Module 是一个 Verilog 模块在内存中的对应物。
- `RTLIL::Module` 内部用 `wires_`（字典）存线网、用 `cells_`（字典）存单元、用 `connections_`（向量）存 `assign` 这类连续赋值。
- `RTLIL::Wire` 描述一根带位宽的连线（几何属性 + 端口方向），**不**描述驱动关系。
- `RTLIL::Cell` 靠 `type` 说明种类（如 `$and`、`$dff`），靠「端口名 → SigSpec」字典表达连接。
- `RTLIL::SigSpec` 是描述「一段信号」的通用语言，能涵盖整根线、切片、常数与拼接。

### 2.2 本讲新增的两个小概念

**(a) 谁来“拥有”对象？**

RTLIL 里的 Wire、Cell、Memory、Process 都不是你自己 `new` 出来就完事的——它们必须被某个 Module **接纳（add）** 才算合法。Module 在 `add()` 时会做两件事：把对象按名字塞进自己的字典，并给对象写上一个指向自己的反向指针 `module`。换句话说，**Module 是这些对象的“容器/拥有者”**，对象离开了 Module 就无法被序列化、也无法参与综合。本讲会反复出现这条规则：**创建对象请用 Module 的 `addWire/addCell` 工厂方法，不要直接 `new`。**

**(b) 名字为什么有前缀？**

u2-l1 已经讲过：RTLIL 标识符必须以 `\`（公有，源自 HDL）或 `$`（Yosys 自动生成，互不冲突）开头。本讲会大量出现 `ID($and)`、`ID::A`、`ID::A_WIDTH`、`NEW_ID` 这样的写法，它们都是用来**安全地拿到一个合法 IdString** 的工具，本讲会在用到时逐一解释。

## 3. 本讲源码地图

本讲只读两个文件，但它们是整个 Yosys 里最核心的两个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [kernel/rtlil.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h) | RTLIL 所有数据结构（Design/Module/Wire/Cell/…）的 **声明** | `AttrObject`、`NamedObject`、`Module`/`Cell`/`Wire` 的成员函数声明、`ObjRange` |
| [kernel/rtlil.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc) | 这些结构的大部分 **实现** | `Module::add/addWire/addCell/connect/fixup_ports`、`Cell::hasPort/getPort/setParam/getParam` |

另外有两个点会顺带引用，但不是本讲主角：

- [kernel/rtlil_bufnorm.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil_bufnorm.cc)：`Cell::setPort` 的实现位于此（与一种叫 buffered-normalized 的内部机制耦合）。
- [kernel/yosys_common.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys_common.h)：`NEW_ID` 宏的定义。

> 提示：本讲刻意不展开 `$and` 这类内部单元的“完整端口表”，那是下一讲 [u3-l4 内部单元库](u3-l4-internal-cell-library.md) 的主题。本讲只关心「怎么把任意一个 Cell 造出来并连上线」这套**通用**编程接口。

---

## 4. 核心概念与源码讲解

### 4.1 Module 构造接口：addWire / addCell / connect / fixup_ports

#### 4.1.1 概念说明

写 Pass 时最常见的诉求是：「在这个 module 里加一根线」「加一个门」「把两个信号连起来」。这三件事分别对应三个工厂/操作方法：

- `addWire(name, width)` —— 新建一根指定宽度的线，并交给 module 拥有。
- `addCell(name, type)` —— 新建一个指定类型的单元（如 `$and`），并交给 module 拥有。
- `connect(lhs, rhs)` —— 增加**一条 module 级的连续赋值**（等价于 Verilog 的 `assign lhs = rhs;`）。

此外，端口（port）是 Wire 上的一组标记（`port_input`/`port_output`/`port_id`），Verilog 要求端口有确定顺序，`fixup_ports()` 就是用来“整理端口顺序、补全 port_id”的方法，前端把 HDL 读进来之后会调用它。

#### 4.1.2 核心流程

构造一个含输入输出端口和小逻辑的 module，典型流程是：

```text
1. module->addWire(\a, 1)   →  线 a，再设 a->port_input = true
2. module->addWire(\b, 1)   →  线 b，再设 b->port_input = true
3. module->addWire(\y, 1)   →  线 y，再设 y->port_output = true
4. module->fixup_ports()    →  按 (input/output, 名字) 排序，给 port_id 编号，填 module->ports
5. RTLIL::Cell *g = module->addCell(NEW_ID, ID($and))   →  创建 $and 门
6. g->setPort(ID::A, a) ; g->setPort(ID::B, b) ; g->setPort(ID::Y, y)   →  连端口（见 4.2）
   —— 注意：门的 Y 已经“驱动”了 y，这里通常不需要再 module->connect
```

需要特别注意两个**不同的“连接”**，初学者最容易混淆：

| 机制 | 入口 | 存到哪里 | 语义 |
| --- | --- | --- | --- |
| module 级连接 | `module->connect(lhs, rhs)` | `Module::connections_`（向量） | 一条 `assign lhs = rhs;`，把 rhs 的驱动“灌”到 lhs |
| cell 级连接 | `cell->setPort(name, sig)` | `Cell::connections_`（字典） | 把 cell 的某个**端口**接到某段信号 sig 上 |

一句话区分：**`module->connect` 是“给一根线赋值”，`cell->setPort` 是“给一个门的管脚接线”。** 一个 `$and` 门的输入输出是通过 `setPort` 接到信号上的，门本身已经表达了「Y = A & B」这个逻辑关系，所以**通常不需要再为它额外 `module->connect`**；只有当你想把某段信号“别名/连”到另一段（比如把常数驱动到线、或把两根线合并）时，才用 `module->connect`。

#### 4.1.3 源码精读

**(a) addWire / addCell：创建 + 交给 module 拥有**

[rtlil.cc:3173-3181](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3173-L3181) 是 `addWire` 的实现：`new` 一个 Wire，设好 `name` 与 `width`，然后调用私有的 `add(wire)`。

```cpp
RTLIL::Wire *RTLIL::Module::addWire(RTLIL::IdString name, int width)
{
    log_assert(width >= 0 && width < RTLIL::WIDTH_LIMIT);
    RTLIL::Wire *wire = new RTLIL::Wire;
    wire->name = std::move(name);
    wire->width = width;
    add(wire);          // 交给 module 拥有
    return wire;
}
```

`addCell` 几乎是镜像，见 [rtlil.cc:3197-3204](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3197-L3204)：`new` 一个 Cell，设 `name` 与 `type`，调用 `add(cell)`。

真正“接纳”的逻辑在私有的 `add()` 重载里，[rtlil.cc:2885-2901](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L2885-L2901)：

```cpp
void RTLIL::Module::add(RTLIL::Wire *wire) {
    log_assert(!wire->name.empty());
    log_assert(count_id(wire->name) == 0);   // 名字在 module 内必须唯一
    log_assert(refcount_wires_ == 0);        // 此时不能有人正在遍历 wires_
    wires_[wire->name] = wire;
    wire->module = this;                     // 写入反向指针
}
```

这段揭示了三条重要规则：

1. **名字唯一**：用 `count_id(name) == 0` 断言。`count_id` 把 wire/cell/memory/process 四类对象当成**同一个命名空间**计数（见下）。
2. **写反向指针**：`wire->module = this`。这就是为什么你拿到一个 Wire*/Cell* 后，能用 `wire->module` 反查它属于哪个 module。
3. **遍历保护**：`refcount_wires_ == 0` 保证“添加时没有任何人正在用迭代器遍历 wires_”——这与本讲后面要讲的 `ObjRange` 直接相关。

> 共享命名空间的证据在 [rtlil.cc:1616-1619](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1616-L1619)（`count_id`）：
> ```cpp
> return wires_.count(id) + memories.count(id) + cells_.count(id) + processes.count(id);
> ```
> 也就是说，**同一个 module 里，一根线和它同名的一个 cell 是非法的**（即使它们类型不同）。这一点和 Verilog 的习惯一致。

**(b) connect：增加一条 module 级赋值**

[rtlil.cc:3086-3115](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3086-L3115) 是 `connect(SigSig)` 的实现。它的核心做了三件事：

```cpp
void RTLIL::Module::connect(const RTLIL::SigSig &conn)
{
    // 1) 通知所有 monitor（监控 design 变化的钩子）
    for (auto mon : monitors) mon->notify_connect(this, conn);

    // 2) 丢弃「把常数赋给常数」的无效赋值，只保留左边是 wire 的位
    if (conn.first.has_const()) { /* 过滤后递归 */ }

    // 3) 断言左右等宽，然后压入 connections_
    log_assert(GetSize(conn.first) == GetSize(conn.second));
    connections_.push_back(conn);
}
```

- `SigSig` 是 `std::pair<SigSpec, SigSpec>`，定义在 [rtlil.h:129](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L129)。两个参数版的 `connect(lhs, rhs)`（[rtlil.cc:3117-3120](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3117-L3120)）只是包了一层转发过来。
- 「左右等宽」是硬约束：你不能把 3 位的信号赋给 2 位的线而不做显式截断。
- `connections_` 里存的就是「写网表时 `connect \lhs rhs` 那一行」对应的内存数据（u2-l1 已讲）。

**(c) fixup_ports：整理端口顺序**

[rtlil.cc:3146-3171](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3146-L3171) 的逻辑可概括为：

```text
1. 收集所有 port_input || port_output 的 wire；非端口的 wire 把 port_id 清零
2. 按固定比较器排序（fixup_ports_compare）
3. 清空 module->ports，按下标给端口 wire 赋 port_id = i+1，并把名字 push 进 ports
```

其中 `module->ports` 是一个 `std::vector<IdString>`，记录端口名字的**声明顺序**（[rtlil.h:2110](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2110)）。`port_id` 则是每个端口 wire 上的 1 起编号。如果你在 Pass 里手工新增了端口 wire，最后记得调一次 `fixup_ports()` 让顺序与编号自洽。

**(d) 遍历：wires() / cells() 与 ObjRange**

拿到 module 后，你几乎一定要遍历它的线或单元。声明在 [rtlil.h:2162-2167](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2162-L2167)：

```cpp
RTLIL::ObjRange<RTLIL::Wire*> wires() { ... }
RTLIL::ObjRange<RTLIL::Cell*> cells() { ... }
```

`ObjRange`（[rtlil.h:979-1009](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L979-L1009)）是对内部字典的**安全包装**：它在迭代期间会递增一个 `refcount`（就是前面 `add()` 断言里出现的 `refcount_wires_`/`refcount_cells_`），从而**在别人正遍历时禁止增删**，避免迭代器失效。它还提供了到 `pool<T>` 和 `std::vector<T>` 的隐式转换，所以你可以直接写：

```cpp
for (RTLIL::Cell *c : module->cells()) { ... }        // range-for
std::vector<RTLIL::Cell*> all = module->cells();      // 整体拷成 vector
```

#### 4.1.4 代码实践

> 实践目标：在真实 Yosys 里观察「一根输入线 + 一根输出线 + 一个 `$and` 门」长什么样，并把它和本节的 `addWire/addCell/connect` 对应起来。

1. 准备一个最小的 Verilog 文件 `and2.v`（**示例代码**，非项目原有文件）：

   ```verilog
   module and2(input a, input b, output y);
       assign y = a & b;
   endmodule
   ```

2. 用交互式 shell 或脚本读入并直接写出 RTLIL 文本（参考 u2-l1 的 `write_rtlil`）：

   ```text
   yosys> read_verilog and2.v
   yosys> write_rtlil and2.rtlil
   ```

3. 打开 `and2.rtlil`，你应该能看到类似下面的结构（具体名字可能因 autoidx 略有不同）：

   ```text
   module \and2
     wire input 1 \a
     wire input 1 \b
     wire output 1 \y
     cell $and $techmap$and_y.$and$and2.v:1$1
       parameter \A_WIDTH 1'00000001
       parameter \B_WIDTH 1'00000001
       parameter \Y_WIDTH 1'00000001
       ...
       connect \A \a
       connect \B \b
       connect \Y \y
     end
   end
   ```

4. **需要观察的现象**：
   - 三根 wire 各自带 `input`/`output` 标记——这正是 `fixup_ports()` 整理出来的端口方向，对应 wire 上的 `port_input`/`port_output` 字段。
   - 门 `$and` 有三个端口连接 `connect \A \a` 等——这对应本节讲的 **cell 级 `setPort`**，**不是** module 级 `connect`。
   - 门名是 `$…$1` 这种 `$` 开头的自动名——对应 `NEW_ID`（见 4.2）。

5. **预期结果**：你能在文本里一一指认出 addWire 造出的三根线、addCell 造出的一个 `$and`、以及三处 setPort。如果 `write_rtlil` 报错或看不到 `$and`，请确认 `read_verilog` 成功（注意 `a & b` 是位与，会被前端直接生成 `$and`，而不是逻辑与 `$logic_and`）。

> 若你尚未本地构建 Yosys，本步骤的运行结果**待本地验证**；你也可以改为纯阅读实践：直接看 `tests/simple/` 下任一含 `assign` 的 `.v` 文件，脑补它综合后的 RTLIL 结构。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `module->add(RTLIL::Wire*)` 被声明为 `protected`（[rtlil.h:2066](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2066)），而 `addWire` 是 `public`？

<details><summary>参考答案</summary>

`add()` 是“裸接纳”：它假定对象已经被正确构造（有合法名字、合法宽度等），只负责“塞进字典 + 写反向指针”。把这种半成品接口暴露出去，使用者很容易造出半残的 Wire。`addWire`/`addCell` 作为工厂方法，封装了「`new` + 设字段 + `add()`」的全过程，是更安全、更推荐的对外面孔。Wire/Cell 的构造函数本身也因同样理由声明为 `protected`/`friend`（见 [rtlil.h:2437-2440](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2437-L2440) 与 [rtlil.h:2506-2510](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2506-L2510)）。

</details>

**练习 2**：假设你在同一个 module 里先 `addWire(\x)`，再 `addCell(\x, ...)`（注意名字相同），会发生什么？

<details><summary>参考答案</summary>

会在 `add()` 的断言 `count_id(wire->name) == 0` 处触发 `log_assert` 失败而终止（Debug 构建下）。因为 wire 与 cell 共享同一个命名空间（`count_id` 同时统计 wire/cell/memory/process，见 [rtlil.cc:1616-1619](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1616-L1619)）。要给新建对象取一个保证不冲突的名字，请用 `NEW_ID`。

</details>

---

### 4.2 Cell 端口与参数：connections_ / parameters

#### 4.2.1 概念说明

一个 Cell 之所以能“工作”，靠两样东西：

1. **端口连接（connections_）**：这个门的每个管脚（如 `$and` 的 `A`/`B`/`Y`）分别接到哪段信号。存放在 `dict<IdString, SigSpec> connections_` 里。
2. **参数（parameters）**：这个门的“配置常数”，比如 `$and` 的位宽 `A_WIDTH`/`B_WIDTH`/`Y_WIDTH`、是否带符号 `A_SIGNED`，或 `$dff` 的宽度 `WIDTH`。存放在 `dict<IdString, Const> parameters` 里。

对应地，Cell 提供两组成对的方法：

| 数据 | 读 | 写 | 判断存在 |
| --- | --- | --- | --- |
| 端口 | `getPort(name)` | `setPort(name, sig)` | `hasPort(name)` |
| 参数 | `getParam(name)` | `setParam(name, value)` | `hasParam(name)` |

此外还有两个便捷点：

- **方向查询**：`input(name)` / `output(name)` / `port_dir(name)` 告诉你某个端口是输入还是输出——这对“只想遍历 cell 的所有输入”非常有用。
- **参数自洽**：`fixup_parameters()` 能根据端口连接的宽度，**自动补全/修正**那些可由宽度推导出的参数，省去你手算位宽。

#### 4.2.2 核心流程：以 `$and` 为例

Yosys 其实**已经内置**了一个创建 `$and` 的便捷方法 `module->addAnd(...)`。它内部做的事，恰好就是「创建 `$and` 并连接端口」的标准范式。我们用它当样板（实现见 4.2.3 的宏）：

```text
RTLIL::Cell* RTLIL::Module::addAnd(name, sig_a, sig_b, sig_y, is_signed, src):
    cell = addCell(name, ID($and))            // 1. 创建单元，类型 $and
    cell->parameters[ID::A_SIGNED] = is_signed  // 2. 写参数：是否带符号
    cell->parameters[ID::B_SIGNED] = is_signed
    cell->parameters[ID::A_WIDTH] = sig_a.size() //   写参数：各端口宽度
    cell->parameters[ID::B_WIDTH] = sig_b.size()
    cell->parameters[ID::Y_WIDTH] = sig_y.size()
    cell->setPort(ID::A, sig_a)                 // 3. 连端口
    cell->setPort(ID::B, sig_b)
    cell->setPort(ID::Y, sig_y)
    cell->set_src_attribute(src)                // 4. 记录源码位置（属性，见 4.3）
    return cell
```

这就是本讲实践任务要你列出的「全部调用序列」。可以看到它严格遵循 **addCell → setParam → setPort** 的次序。

> 这里的 `ID($and)`、`ID::A`、`ID::A_WIDTH` 是什么？`ID(...)` 是一个宏（[rtlil.h:740](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L740)），把一个标识符串安全地包成一个 `IdString`；而 `ID::A`、`ID::A_WIDTH` 等是预先内部化好的静态 `IdString` 常量（在 `constids.inc` 里集中登记，u3-l4 会详讲）。在本讲你只要知道：**它们都是合法的端口名/参数名 IdString，用它们能避免拼写错误。**

#### 4.2.3 源码精读

**(a) Cell 的成员布局**

先看 Cell 结构本身，[rtlil.h:2501-2520](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2501-L2520)：

```cpp
struct RTLIL::Cell : public RTLIL::NamedObject {
    RTLIL::Module *module;                                  // 反向指针：属于哪个 module
    RTLIL::IdString type;                                   // 单元类型，如 $and / $dff / \子模块名
    dict<RTLIL::IdString, RTLIL::SigSpec> connections_;     // 端口连接（端口名 → 信号）
    dict<RTLIL::IdString, RTLIL::Const> parameters;         // 参数（参数名 → 常数）
    ...
};
```

要点：

- Cell 同时持有 `module` 反向指针（由 `Module::add(cell)` 写入，见 4.1.3）和自己的 `type`。
- `connections_` 与 `parameters` 都是 hashlib 的 `dict`（u3-l3 会讲它的性能特点）。
- Cell 同样继承自 `NamedObject`，所以也有 `name` 和 `attributes`（见 4.3）。

**(b) 端口读写：hasPort / getPort / setPort**

`hasPort` 与 `getPort` 很短，[rtlil.cc:4349-4364](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4349-L4364)：

```cpp
bool RTLIL::Cell::hasPort(RTLIL::IdString portname) const {
    return connections_.count(portname) != 0;
}
const RTLIL::SigSpec &RTLIL::Cell::getPort(RTLIL::IdString portname) const {
    return connections_.at(portname);   // 找不到会抛 std::out_of_range
}
```

注意 `getPort` 用的是 `.at()`——**端口不存在时会抛异常**。所以遍历陌生 cell 的端口前，更稳妥的是先 `hasPort` 判断，或用 `connections()` 拿到整张字典再遍历。

`setPort` 的实现较重，位于 [rtlil_bufnorm.cc:589](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil_bufnorm.cc#L589) 起。它的核心逻辑是：

```cpp
void RTLIL::Cell::setPort(RTLIL::IdString portname, RTLIL::SigSpec signal) {
    auto r = connections_.insert(portname);          // 插入（或定位到已有项）
    if (!r.second && r.first->second == signal)      // 若已存在且值相同
        return;                                      //   则什么都不做（短路优化）
    // 通知 monitor；若启用 buffered-normalized，还要维护“哪根线被谁驱动”的索引
    ...
    r.first->second = std::move(signal);             // 最终写入新连接
}
```

对 Pass 编写者来说，关键结论是：

- `setPort` 最终写入的就是 `connections_` 这张字典；
- 它会**通知 monitor**（与 `module->connect` 一样），所以 GUI/调试钩子能感知到端口变化；
- 它内部还参与一种叫 **buffered-normalized** 的驱动索引维护（用于增量归一化信号名）。你不必深究这套机制，只要记住：**改端口请走 `setPort`，不要直接改 `connections_`**，否则会绕过通知与索引维护，留下不一致状态。

**(c) 参数读写：setParam / getParam 的“默认值回退”**

[rtlil.cc:4425-4441](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4425-L4441)：

```cpp
void RTLIL::Cell::setParam(RTLIL::IdString paramname, RTLIL::Const value) {
    parameters[paramname] = std::move(value);
}
const RTLIL::Const &RTLIL::Cell::getParam(RTLIL::IdString paramname) const {
    const auto &it = parameters.find(paramname);
    if (it != parameters.end())
        return it->second;
    // 关键：本 cell 没设这个参数时，回退去查“类型对应的模块”的默认值
    if (module && module->design) {
        RTLIL::Module *m = module->design->module(type);
        if (m) return m->parameter_default_values.at(paramname);
    }
    throw std::out_of_range("Cell::getParam()");
}
```

这里有一个非常巧妙的设计：`getParam` 找不到时，会去 `design` 里查「与 cell 同名（同 type）的模块」的 `parameter_default_values`。这对应 Verilog 里 `module #(.WIDTH(8))` 这种参数化模块——当实例没显式给参数时，就用模块定义里的默认值。**这也解释了为什么 `$and` 这类内部单元的 type `$and` 同时也是 design 里的一个（黑盒）模块名。**

**(d) 参数自洽：fixup_parameters**

[rtlil.cc:4458](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4458) 起的 `fixup_parameters()`，能让你**只设端口、不手算宽度**。它的开头是这样的（[rtlil.cc:4458-4470](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4458-L4470)）：

```cpp
void RTLIL::Cell::fixup_parameters(bool set_a_signed, bool set_b_signed) {
    if (!type.begins_with("$") || ...) return;        // 非内部 $ 单元直接跳过
    if (type == ID($mux) || type == ID($pmux) ...) {
        parameters[ID::WIDTH] = GetSize(connections_[ID::Y]);   // 由 Y 的宽度反推 WIDTH
        ...
        return;
    }
    ...
}
```

也就是说，对于 `$mux` 这类单元，你只要 `setPort` 好端口，再调 `fixup_parameters()`，它就会根据端口宽度把 `WIDTH` 等参数补齐。这在写 techmap 这类需要批量造单元的 Pass 时极其方便。

**(e) 样板：addAnd 是怎么用上面这套接口的**

`addAnd` 并不是手写的函数，而是用宏 `DEF_METHOD` 批量生成的，定义在 [rtlil.cc:3295-3317](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3295-L3317)：

```cpp
#define DEF_METHOD(_func, _y_size, _type)                                          \
    RTLIL::Cell* RTLIL::Module::add##_func(RTLIL::IdString name,                   \
            const RTLIL::SigSpec &sig_a, const RTLIL::SigSpec &sig_b,             \
            const RTLIL::SigSpec &sig_y, bool is_signed, const std::string &src) { \
        RTLIL::Cell *cell = addCell(name, _type);           /* addCell */         \
        cell->parameters[ID::A_SIGNED] = is_signed;         /* setParam 风格 */   \
        cell->parameters[ID::B_SIGNED] = is_signed;                               \
        cell->parameters[ID::A_WIDTH] = sig_a.size();                             \
        cell->parameters[ID::B_WIDTH] = sig_b.size();                             \
        cell->parameters[ID::Y_WIDTH] = sig_y.size();                             \
        cell->setPort(ID::A, sig_a);                        /* setPort */         \
        cell->setPort(ID::B, sig_b);                                              \
        cell->setPort(ID::Y, sig_y);                                              \
        cell->set_src_attribute(src);                                             \
        return cell;                                                              \
    } ...
DEF_METHOD(And, max(sig_a.size(), sig_b.size()), ID($and))    // ← addAnd 由这一行生成
```

> 注意：`cell->parameters[...] = ...` 是直接对字典赋值，等价于 `setParam`。Yosys 内部代码两种写法都常见：在“确知构造中、且需要批量设值”时直接用 `[]`，在“可能改动已存在 cell、需要通知”时用 `setParam`/`setPort`。

`name` 这个参数填什么？大多数内部中间单元没有有意义的名字，于是用 `NEW_ID` 生成一个保证唯一的 `$…` 自动名。[yosys_common.h:303-307](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys_common.h#L303-L307) 定义了它，本质是调用 `new_autoidx_with_prefix(...)` 取一个自增的 `$<前缀>$<序号>` 名字。

真实使用示例见 [kernel/compressor_tree.cc:63](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/compressor_tree.cc#L63)：

```cpp
module->addAnd(NEW_ID, b_shifted, ai_rep, row);
```

这正是「用 `NEW_ID` 当名字、传入三个 SigSpec、由 `addAnd` 内部完成 addCell+setParam+setPort」的典型一行调用。

#### 4.2.4 代码实践

> 实践目标：把 4.1 实践里看到的那个 `$and` 单元，**反向**拆解成 `addAnd`/`addCell+setParam+setPort` 的调用序列，验证你对源码的理解。

1. 重新打开 4.1 实践生成的 `and2.rtlil`，定位到那个 `cell $and …` 块。
2. 列一张表，把文本里的每一行映射到一次 C++ 调用：

   | RTLIL 文本 | 对应的 C++ 调用（addAnd 内部） | 出处 |
   | --- | --- | --- |
   | `cell $and $…` | `addCell(name, ID($and))` | rtlil.cc:3297 |
   | `parameter \A_WIDTH …` | `cell->parameters[ID::A_WIDTH] = sig_a.size()` | rtlil.cc:3300 |
   | `parameter \B_WIDTH …` | `cell->parameters[ID::B_WIDTH] = sig_b.size()` | rtlil.cc:3301 |
   | `parameter \Y_WIDTH …` | `cell->parameters[ID::Y_WIDTH] = sig_y.size()` | rtlil.cc:3302 |
   | `connect \A \a` | `cell->setPort(ID::A, sig_a)` | rtlil.cc:3303 |
   | `connect \B \b` | `cell->setPort(ID::B, sig_b)` | rtlil.cc:3304 |
   | `connect \Y \y` | `cell->setPort(ID::Y, sig_y)` | rtlil.cc:3305 |

3. **需要观察的现象**：文本里参数的**位宽**（`A_WIDTH` 等）应该正好等于对应端口信号的宽度（这里都是 1）。把 Verilog 里的位宽改成 `input [3:0] a` 重新跑一遍，观察 `A_WIDTH` 变成 4，验证「参数由 `sig_a.size()` 决定」。

4. **预期结果**：你能不查源码地说出「一个 `$and` 单元 = 1 次 addCell + 5 次 setParam + 3 次 setPort」。

5. **进阶（可选，待本地验证）**：把脚本改成

   ```text
   yosys> read_verilog and2.v
   yosys> write_rtlil
   yosys> select and2/t:*        # 选中所有 cell（通配，u4-l3 详讲）
   yosys> show                   # 或 dump，查看 cell
   ```

   并尝试用 `getattr`/属性查看（或直接看文本）确认 `A_SIGNED` 默认是 0（即 `is_signed=false`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么推荐用 `cell->setPort(name, sig)` 而不是 `cell->connections_[name] = sig`？

<details><summary>参考答案</summary>

`setPort`（[rtlil_bufnorm.cc:589](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil_bufnorm.cc#L589)）在写入之外还做了两件重要的事：(1) 通知 module/design 的 monitor，让依赖变更通知的机制（如 GUI、增量分析）保持一致；(2) 在启用 buffered-normalized 时维护「线被谁驱动」的索引。直接改字典会绕过这些，可能留下不一致的内部状态。唯一可以放心直接写 `parameters[..]`/`connections_[..]` 的场景，是在**全新构造**一个 cell、且随后不会再被增量机制触碰时——这也是 `addAnd` 宏里直接赋值的原因。

</details>

**练习 2**：`cell->getParam(ID::FOO)` 在两种情况下会“成功返回”却可能来自不同地方，是哪两种？

<details><summary>参考答案</summary>

(1) 该 cell 自己在 `parameters` 里显式设过 `FOO`，直接返回它；(2) 没设过，但 design 里存在一个与 cell 同 `type` 的模块，且它的 `parameter_default_values` 里有 `FOO`，则回退返回该默认值（[rtlil.cc:4430-4441](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4430-L4441)）。两种都查不到才抛 `std::out_of_range`。

</details>

---

### 4.3 属性系统：AttrObject

#### 4.3.1 概念说明

除了「结构」（谁连谁），RTLIL 对象还经常需要携带一些**元信息**——比如「这根线请保留别优化掉」「这个 cell 来自源文件第几行」「这个模块是黑盒」。这些元信息就是**属性（attribute）**。

Yosys 把属性能力抽到一个基类 `RTLIL::AttrObject` 里，并让几乎所有 IR 对象（Module/Wire/Cell/Memory/Process 以及 Process 内部的 CaseRule/SwitchRule）都继承它。属性统一存成一张 `dict<IdString, Const> attributes`——也就是说，**属性名是一个 IdString，属性值是一个 RTLIL::Const（位向量常数）**。这意味着属性既能表达布尔（`Const(1)`）、也能表达整数、字符串。

`NamedObject` 在 `AttrObject` 之上只多加了一个 `name` 字段，所以「有名 + 有属性」成了 Module/Wire/Cell 的共同基底。

#### 4.3.2 核心流程

属性系统的接口可以分成几档：

| 类型 | 写 | 读 | 典型用途 |
| --- | --- | --- | --- |
| 布尔 | `set_bool_attribute(id)` | `get_bool_attribute(id)` | `keep`（别删）、`blackbox`（黑盒）、`dynreduce` 等 |
| 字符串 | `set_string_attribute(id, s)` | `get_string_attribute(id)` | `src`（源码定位）、`hdlname` |
| 字符串集合 | `set_strpool_attribute` / `add_strpool_attribute` | `get_strpool_attribute` | 多值标签 |
| 整数向量 | `set_intvec_attribute` | `get_intvec_attribute` | 一些需要存多个数的标记 |
| 任意 | 直接 `attributes[id] = Const(...)` | `attributes.at(id)` | 兜底 |

其中 `src` 属性用得最多，专门记录「这一条 RTLIL 来自哪个文件的哪一行」，方便报错与调试。为此 `AttrObject` 还提供了便捷的 `set_src_attribute(str)` / `get_src_attribute()`。

#### 4.3.3 源码精读

**(a) 继承体系：AttrObject ← NamedObject ← Module/Wire/Cell**

[rtlil.h:1261-1299](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1261-L1299) 把这套基底讲得很清楚：

```cpp
struct RTLIL::AttrObject {
    dict<RTLIL::IdString, RTLIL::Const> attributes;   // 属性表：名字 → 常数
    bool has_attribute(RTLIL::IdString id) const;
    void set_bool_attribute(RTLIL::IdString id, bool value=true);
    bool get_bool_attribute(RTLIL::IdString id) const;
    void set_string_attribute(RTLIL::IdString id, string value);
    string get_string_attribute(RTLIL::IdString id) const;
    void set_src_attribute(const std::string &src)  { set_string_attribute(ID::src, src); }
    std::string get_src_attribute() const           { return get_string_attribute(ID::src); }
    // ... strpool / intvec / hdlname ...
};

struct RTLIL::NamedObject : public RTLIL::AttrObject {
    RTLIL::IdString name;     // 只多了一个名字
};
```

而 `Module`（[rtlil.h:2060](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2060)）、`Wire`（[rtlil.h:2431](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2431)）、`Cell`（[rtlil.h:2501](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2501)）都继承自 `NamedObject`，因此它们**都有 `name` 和 `attributes`**。

**(b) 布尔属性 = 「值为 1 的 Const」**

`set_bool_attribute` 的实现非常直白，[rtlil.cc:932-946](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L932-L946)：

```cpp
void RTLIL::AttrObject::set_bool_attribute(RTLIL::IdString id, bool value) {
    if (value)
        attributes[id] = RTLIL::Const(1);   // 真：写入值为 1 的常数
    else
        attributes.erase(id);               // 假：直接删掉这一项
}
bool RTLIL::AttrObject::get_bool_attribute(RTLIL::IdString id) const {
    const auto it = attributes.find(id);
    if (it == attributes.end()) return false;
    return it->second.as_bool();            // 把常数当布尔解释
}
```

这里有个**重要实现细节**：设 `false` 不是写一个「值为 0」的属性，而是**直接删除该属性**。所以在你自己的 Pass 里，判断「某布尔属性是否存在」和「它是否为真」是等价的。这也意味着你不能用布尔属性来可靠地区分「显式设为 false」与「从未设过」——二者都是“不在表里”。

**(c) 属性在实践中的真实身影**

- `Cell` 上的 `has_keep_attr()`（[rtlil.h:2545-2548](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2545-L2548)）就是用 `get_bool_attribute(ID::keep)` 判断「这个 cell 或它的类型模块有没有 `keep` 属性」，从而决定优化 Pass 是否可以删掉它。
- 前面 `addAnd` 宏里最后一行 `cell->set_src_attribute(src)`，就是把来源字符串写到 `src` 属性上——这就是为什么 `write_rtlil` 输出里常能看到 `attribute \src "and2.v:1"`。

#### 4.3.4 代码实践

> 实践目标：亲手给一个 wire 打上布尔属性，并用 `write_rtlil` 验证属性确实落到了文本里。

1. 把 4.1 的 Verilog 改成带 `(* keep *)` 的形式（**示例代码**）：

   ```verilog
   module and2(input a, input b, output y);
       (* keep *) wire y;
       assign y = a & b;
   endmodule
   ```

   `(* keep *)` 是 Verilog 标准的属性语法，前端会把它翻译成 wire 上的 `keep` 属性。

2. 重新 `read_verilog` 并 `write_rtlil`。

3. **需要观察的现象**：在 `\y` 这根 wire 的文本里应多出一行 `attribute \keep 1`。这正是 `set_bool_attribute(ID::keep)` 写入的「值为 1 的 Const」的文本表现。

4. **预期结果**：你能看到属性被序列化；若删掉 `(* keep *)` 再跑，这行消失——对应 `set_bool_attribute(id, false)` 的「删除”语义。

5. **源码阅读延伸**：在 [rtlil.cc:932-938](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L932-L938) 处确认「设 false = 删除」后，思考：如果某个 Pass 想表达「这个 wire 我特意算过、它确实不该有 keep」，它能否用布尔属性 `keep=false` 来表达？为什么？

> 若未本地构建，属性文本的具体行**待本地验证**；纯阅读替代方案：在仓库里搜索 `set_bool_attribute(ID::` 的真实使用点（如各优化 Pass），观察它们给谁打了什么属性。

#### 4.3.5 小练习与答案

**练习 1**：Module、Wire、Cell 都能调用 `set_bool_attribute`，为什么？它们各自继承自谁？

<details><summary>参考答案</summary>

因为三者都继承自 `RTLIL::NamedObject`，而 `NamedObject` 继承自 `RTLIL::AttrObject`（[rtlil.h:1296-1299](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1296-L1299)），`set_bool_attribute` 是 `AttrObject` 的成员。所以属性能力是「所有 IR 对象的公共底座」，不因对象种类而异。

</details>

**练习 2**：`get_bool_attribute` 返回 `false` 时，能否区分「属性被显式设为 false」和「属性根本不存在」？

<details><summary>参考答案</summary>

不能。因为 `set_bool_attribute(id, false)` 的实现是 `attributes.erase(id)`（[rtlil.cc:936-937](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L936-L937)），两种情况都表现为「表中无此项」，`get_bool_attribute` 都返回 `false`。需要区分时，应改用 `has_attribute(id)` 或换一种属性表示。

</details>

---

## 5. 综合实践

> 把本讲三块知识串起来：**用“人脑解释器”手工综合一个小设计，并写出它对应的 RTLIL 构造代码**。

设计如下（**示例代码**）：

```verilog
module mux2(input a, input b, input sel, output y);
    assign y = sel ? b : a;
endmodule
```

请完成：

1. **预测结构**（用本讲的语言）：
   - 应该有几根 wire？哪些是 `port_input`，哪个是 `port_output`？
   - 会被前端生成哪种内部单元？（提示：二选一多路器是 `$mux`，端口为 `A`/`B`/`S`/`Y`，参数有 `WIDTH` 与 `S_WIDTH`）
   - 这个单元的端口分别接到哪些 SigSpec？

2. **写出构造序列**（伪 C++，参考 4.2 的 addAnd 范式）：
   - 列出需要的 `module->addWire(...)` 调用，并标出哪根要设 `port_input/port_output`。
   - 写出 `module->addCell(NEW_ID, ID($mux))` 之后的 `setPort` 序列。
   - 思考：这里需要 `module->connect(...)` 吗？为什么（结合 4.1.2 的「两种连接」）？

3. **用工具验证**：`read_verilog` 读入上面的设计 → `write_rtlil` → 对照你的预测，逐项核对 wire 数量、端口方向、`$mux` 的端口与参数（特别注意 `S_WIDTH` 应为 1，`WIDTH` 应为 1）。

4. **加分项**：尝试只 `setPort` 不设参数，再对该 cell 调用 `cell->fixup_parameters()`，然后用 `dump` 或 `write_rtlil` 观察 `WIDTH`/`S_WIDTH` 是否被自动补齐（对应 4.2.3 的 (d)）。

完成本任务后，你就具备了「读懂任意一段构造 RTLIL 的 C++ 代码」的能力，这正是 [u9-l1 编写你的第一个自定义 Pass](u9-l1-write-custom-pass.md) 的直接前置。

## 6. 本讲小结

- **构造靠工厂方法**：用 `module->addWire(name, width)` / `addCell(name, type)` 创建对象，它们内部调用私有 `add()`，完成「塞进字典 + 写 `module` 反向指针 + 名字唯一性断言」（[rtlil.cc:2885-2901](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L2885-L2901)、[rtlil.cc:3173-3204](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3173-L3204)）。wire/cell/memory/process 共享同一命名空间。
- **两种连接不要混**：`module->connect(lhs,rhs)` 写 module 级 `assign`（存 `connections_`）；`cell->setPort(name,sig)` 接 cell 的管脚（存 `cell->connections_`）。门自身已表达逻辑关系，通常无需再 `connect`。
- **Cell = 端口连接 + 参数**：`connections_`（`dict<IdString,SigSpec>`）存管脚接线，`parameters`（`dict<IdString,Const>`）存配置常数；读写分别用 `hasPort/getPort/setPort` 与 `hasParam/getParam/setParam`。
- **`getParam` 会回退默认值**：本 cell 没设的参数，会去 design 里「同 type 模块」的 `parameter_default_values` 取（[rtlil.cc:4430-4441](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4430-L4441)）。
- **样板 addAnd = addCell + 5×setParam + 3×setPort**：由宏批量生成（[rtlil.cc:3295-3317](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L3295-L3317)）；`fixup_parameters()` 可由端口宽度自动补齐参数。
- **属性是公共底座**：`AttrObject` 提供 `attributes` 表与 `set_bool_attribute` 等；设 `false` 等于删除；Module/Wire/Cell 因都继承 `NamedObject ← AttrObject` 而都拥有属性能力。

## 7. 下一步学习建议

- 本讲只关心「造一个 Cell 并接线」，但没有展开 `$and`/`$mux`/`$dff` 这些**内部单元到底有哪些、各自端口和参数叫什么**。这正是下一讲 [u3-l4 Yosys 内部单元库：celltypes、constids、newcelltypes](u3-l4-internal-cell-library.md) 的主题，建议紧接着读。
- 若你对 `IdString`、`Const`、`dict`/`pool` 这些「名字与常数、高性能容器」的底层实现好奇，可以跳读 [u3-l3 IdString、Const 与 hashlib](u3-l3-idstring-const-hashlib.md)。
- 想直接动手写一个遍历 module/cells 的 Pass？那是 [u9-l1 编写你的第一个自定义 Pass](u9-l1-write-custom-pass.md)，本讲的 `addCell/setPort` 序列会在那里被真实组装进一个可加载插件。
- 想理解信号在多处表示下如何归一，请继续 [u3-l2 SigSpec / SigBit / SigChunk 与 sigtools](u3-l2-sigspec-sigtools.md)。
