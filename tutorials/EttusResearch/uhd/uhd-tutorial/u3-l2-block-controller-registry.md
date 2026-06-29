# Block 控制器与注册表

## 1. 本讲目标

上一讲（u3-l1）我们建立了 RFNoC 的「会话入口」认知：`rfnoc_graph::make` 打开设备后，会**枚举 FPGA 上的所有块**，并为每一块造一个 C++「块控制器」对象。那么——

- 块控制器到底是什么？它由哪个基类定义？
- 框架怎么知道某个 NoC ID 对应哪个 C++ 类？
- 一个块在软件里用什么字符串寻址？这个字符串怎么拼？
- 块的「说明书」（端口、参数、寄存器）写在哪里？

本讲拆解这四个问题，对应四个最小模块：`noc_block_base`（块控制器基类）、`block_id`（软件寻址）、`blockdef`（块描述）、`registry`（工厂注册表）。学完后你应该能：

1. 说清 `noc_block_base` 在 RFNoC 软件栈中的角色与它管理哪些状态。
2. 写出一个合法的 `block_id_t` 字符串并解释 `DEVICE/BLOCKNAME#COUNTER` 三段含义。
3. 理解 NoC ID → C++ 类 的「自注册 + 查表」实例化链路。
4. 辨明 `blockdef` 描述机制在现代 UHD 主机驱动中的真实地位（这是一个容易踩坑的点）。

---

## 2. 前置知识

阅读本讲前，请确认你已经理解 u3-l1 中引入的以下概念（本讲直接承接，不再重述）：

- **块（block）**：FPGA 内部一个独立的功能单元（如 Radio、DDC、FFT）。
- **NoC ID**：32 位整数，标识块的**类型**。同一类型的所有块 NoC ID 相同（如所有 DDC 块都是 `0xDDC00000`）。
- **block_id**：标识块的**实例**，形如 `0/DDC#1`。
- **CHDR**：块之间搬运样本的包格式。
- **`rfnoc_graph`**：会话入口，负责枚举块、造控制器、连接流图。

补充一个本讲要用到的术语：

- **块控制器（block controller）**：运行在**主机**用户空间的 C++ 对象，是 FPGA 里某个硬件块的「软件代理」。你在主机上调用的 `set_rx_frequency`、`set_scaling` 等方法，最终都经由块控制器翻译成对该硬件块的寄存器读写。块控制器 ≠ 硬件块，前者是软件，后者是 FPGA 逻辑，二者通过控制总线对话。

- **自注册（self-registration）**：UHD 不维护一张「所有块类型」的中央清单。每个块的 `.cpp` 文件里写一个静态初始化块，在 `main` 之前把自己登记进一张全局表。这种模式和 u2-l1 讲过的 `device::register_device` 完全同构，如果你读过那一讲，这里会非常熟悉。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [host/include/uhd/rfnoc/noc_block_base.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp) | 块控制器**基类**的公共声明。定义所有块控制器共享的接口与状态。 |
| [host/include/uhd/rfnoc/block_id.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/block_id.hpp) | `block_id_t` 类声明——块的软件寻址。 |
| [host/lib/rfnoc/block_id.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_id.cpp) | `block_id_t` 的实现：字符串解析、正则校验、`match` 匹配。 |
| [host/include/uhd/rfnoc/blockdef.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/blockdef.hpp) | 块**描述符**的公共声明（端口、参数、寄存器）。 |
| [host/include/uhd/rfnoc/registry.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/registry.hpp) | 注册宏与 `registry` 类——把「NoC ID → 工厂函数」登记进表。 |
| [host/lib/rfnoc/registry_factory.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/registry_factory.cpp) | 两张注册表与查表逻辑的实现。 |
| [host/include/uhd/rfnoc/defaults.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/defaults.hpp) | 类型别名（`noc_id_t`、`device_type_t`）与所有内置块的 NoC ID 常量。 |
| [host/include/uhd/rfnoc/constants.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/constants.hpp) | block_id 字符串校验用的正则表达式。 |
| [host/include/uhd/rfnoc/blocks/ddc.yml](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/blocks/ddc.yml) | DDC 块的 YAML 描述（RFNoC 工具链用）。 |
| [host/lib/rfnoc/ddc_block_control.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp) | DDC 块控制器实现，含真实注册调用。 |

---

## 4. 核心概念与源码讲解

### 4.1 noc_block_base 基类

#### 4.1.1 概念说明

`noc_block_base` 是**所有块控制器的基类**。每写一个新块（比如 DDC、FFT），就派生一个 `xxx_block_control_impl` 类继承它。它解决一个问题：**统一所有块的「软件代理」骨架**，让框架可以用同一套代码管理千差万别的块。

它继承自两个父类，分别提供两套能力：

- `node_t`：提供**属性（property）系统**。块暴露给外部世界的可配置项（采样率、增益、缩放系数等）都建模为属性节点，能在块与块之间自动传播（u3-l5 会专讲 experts 属性传播）。
- `register_iface_holder`：提供**底层寄存器访问**。块控制器通过它读写 FPGA 里该硬件块的寄存器。

注释把这种关系说得很清楚：

> The main difference between this class and its parent is the direct access to registers, and the NoC-block IDs.
> —— [noc_block_base.hpp:31-40](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L31-L40)

也就是说：父类 `node_t` 只懂「属性」，而 `noc_block_base` 额外懂「寄存器」和「NoC 块身份」。这正好对应 RFNoC 设计哲学——**高级用属性，低级用寄存器**。

#### 4.1.2 核心流程

一个块控制器的生命周期：

```text
工厂函数 factory_fn(make_args)
        │  std::make_shared<xxx_impl>(make_args)
        ▼
noc_block_base 受保护构造函数  ←── 初始化 _noc_id / _block_id / 端口数 / 属性树子树
        │
        ▼
子类构造函数体  ←── 读寄存器、注册自己的属性、设 MTU 策略
        │
        ▼
框架调用 post_init()  ←── 构造结束后的二次校验
        │
        ▼
（被加入 graph 的 block_container，对外可用）
        │   …… 用户通过 graph::get_block<xxx>() 取回强类型句柄 ……
        ▼
会话结束时框架调用 deinit()  ←── 安全停机（停止产生样本等）
        │
        ▼
析构  ←── regs() 已失效，再访问只打日志不动作
```

注意一个关键设计：构造函数是 `protected` 的（[noc_block_base.hpp:232](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L232)），你**不能也不应**直接 `new` 一个块控制器——它只能由框架内部的工厂函数造出来。这保证了每个块的身份（block_id、NoC ID、端口数）都由框架统一分配，不会出现两个块抢同一个 ID 的情况。

#### 4.1.3 源码精读

**类声明与继承关系：**

```cpp
class UHD_API noc_block_base : public node_t, public register_iface_holder
```

见 [noc_block_base.hpp:41-41](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L41-L41)。双继承，`node_t` 给属性系统，`register_iface_holder` 给寄存器访问。

**核心身份与端口状态**（私有成员，构造时由框架填入）：

```cpp
noc_id_t _noc_id;            // 块类型 ID（如 0xDDC00000）
block_id_t _block_id;        // 块实例 ID（如 0/DDC#0）
size_t _num_input_ports;     // 输入端口数，来自 FPGA 全局寄存器空间
size_t _num_output_ports;    // 输出端口数，同上
```

见 [noc_block_base.hpp:364-376](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L364-L376)。注意注释里强调：端口数「passed into this block from the information stored in the global register space」——也就是说**端口数是硬件告诉软件的**，不是软件拍脑袋决定的。子类只能往下调（见 `set_num_input_ports` 的注释，[noc_block_base.hpp:234-243](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L234-L243)），不能往上调。

**三套对外接口**——块对外暴露数据有三条通道（见类注释 [noc_block_base.hpp:31-40](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L31-L40)）：

1. 低级寄存器访问（来自 `register_iface_holder`，用 `regs()`）。
2. 高级属性访问（来自 `node_t`）。
3. 动作执行（action，也是 `node_t` 提供）。

**几个最常用的 public 方法：**

```cpp
noc_id_t get_noc_id() const { return _noc_id; }            // 行 116
const block_id_t& get_block_id() const { return _block_id; } // 行 125
double get_tick_rate() const;                              // 行 134，时间基准的时钟频率
size_t get_mtu(const res_source_info& edge);               // 行 152，某条边的最大包长
uhd::device_addr_t get_block_args() const { return _block_args; } // 行 201，构造时传入的参数
uhd::property_tree::sptr& get_tree() const { return _tree; }      // 行 207，本块专属属性子树
std::shared_ptr<mb_controller> get_mb_controller();        // 行 229，主板控制器（需申请授权）
```

其中 `get_mb_controller()` 很特别：它可能返回 `nullptr`。块必须在注册时**申请** `mb_access=true` 才可能拿到主板控制器（典型例子是 Radio 块，它要控制时钟和子板）。见 [noc_block_base.hpp:218-229](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L218-L229)。

**MTU 转发策略（一个体现 RFNoC 设计细节的点）：**

```cpp
void set_mtu_forwarding_policy(const forwarding_policy_t policy); // 行 296
```

块的输入/输出端口之间，MTU（最大传输单元）如何相互影响，有四种策略：`DROP`（互不影响）、`ONE_TO_ONE`（输入直通到输出，DDC/DUC 用这个把 MTU 透传给 Radio）、`ONE_TO_ALL`（所有端口同一值）、`ONE_TO_FAN`（一个输入扇出到所有输出）。默认 `ONE_TO_ONE`，见 [noc_block_base.hpp:268-296](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L268-L296) 与私有成员 `_mtu_fwd_policy`（[noc_block_base.hpp:383](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L383-L383)）。

**构造参数的不透明指针（ABI 友好设计）：**

```cpp
struct make_args_int_t;                                 // 前向声明，定义藏在 .cpp 里
using make_args_ptr = std::unique_ptr<make_args_int_t, make_args_deleter>; // 行 72
```

见 [noc_block_base.hpp:57-72](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L57-L72)。构造块用的参数包 `make_args_int_t` 是**完全不透明**的——结构体定义不在公共头里。这样以后往参数包里加字段不会破坏 ABI（`libuhd.so` 的二进制兼容）。这是给 OOT（Out-of-Tree）块开发者留的兼容空间，注释里明说 `make_args_t` 这个旧别名是 deprecated 的占位（[noc_block_base.hpp:46-55](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L46-L55)）。

#### 4.1.4 代码实践

**实践目标：** 通过一个真实的块控制器实现，看清「继承 `noc_block_base` + 在构造函数里干活」的标准写法。

**操作步骤：**

1. 打开 [host/lib/rfnoc/ddc_block_control.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp)，定位到文件末尾的注册宏（约 698 行）。
2. 往上翻，找到 `ddc_block_control_impl` 的构造函数，观察它如何调用 `set_mtu_forwarding_policy`、注册属性。
3. 对比 `get_num_input_ports()` / `get_num_output_ports()` 的注释，确认端口数来源。

**需要观察的现象：**

- 构造函数体内**没有**直接 `set_num_input_ports(N)` 来「声明」端口数——端口数是基类从框架拿到的，子类默认沿用。
- DDC 的 MTU 策略应当是 `ONE_TO_ONE`（因为 DDC 要把 MTU 透传给 Radio），这与基类注释里举的例子一致。

**预期结果：** 你会看到一个典型块控制器的骨架：构造时读寄存器初始化状态 → 注册若干属性 resolver → 末尾用宏自注册。这是后续 u3-l6 讲常用块时的统一模板。

> 说明：本实践为源码阅读型，不依赖硬件，也无需编译。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `noc_block_base` 的构造函数是 `protected` 而不是 `public`？

**参考答案：** 因为块的身份（`_noc_id`、`_block_id`、端口数、属性子树）必须由框架统一分配，才能保证「同一图内没有两个块 block_id 冲突」。如果允许外部直接构造，就可能造出身份非法或重复的块。把构造函数设为 `protected`，只有框架内部的工厂函数（友元 `block_initializer`，[noc_block_base.hpp:336](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L336-L336)）和派生子类能调用，从源头杜绝误用。

**练习 2：** Radio 块调用 `get_mb_controller()` 可能返回 `nullptr`，那么它要拿到主板控制器，注册时必须做什么？

**参考答案：** 在注册宏里把 `mb_access` 参数设为 `true`（见 4.4 节 Radio 的真实注册调用）。即便如此，UHD 也**不保证**一定授权，所以代码里仍要检查返回指针是否有效。

---

### 4.2 block_id 标识

#### 4.2.1 概念说明

上一节提到，框架给每个块分配了一个 `_block_id`。这个值是什么？怎么拼？这就是 `block_id_t` 要解决的问题。

回忆 u3-l1：硬件里块用 **NoC ID** 标识类型，软件里用 **block_id** 标识实例。NoC ID 是「它是哪种块」，block_id 是「它是哪一块」。一台设备上可能有两块 DDC（NoC ID 都是 `0xDDC00000`），但它们的 block_id 不同：`0/DDC#0` 和 `0/DDC#1`。

`block_id_t` 是一个三段式标识，格式为：

```text
DEVICE / BLOCKNAME # COUNTER
```

- **DEVICE**：设备号，通常是主板索引（0、1、2……）。
- **BLOCKNAME**：块名，如 `FFT`、`DDC`、`Radio`。
- **COUNTER**：同设备上同名块的序号，从 0 起。

举例：`0/FFT#1` 表示「第 0 号设备上的第 2 个 FFT 块」（计数从 0 开始，所以 `#1` 是第二个）。这个例子直接来自头文件的文档注释。

#### 4.2.2 核心流程

`block_id_t` 的核心能力有两个：

1. **解析**：把字符串 `"0/FFT#1"` 拆成三段存进成员。反过来也能 `to_string()` 拼回去。
2. **匹配（match）**：用「更宽松」的规则判断一个字符串是否指代本块。这是 `graph::get_block("FFT")` 能找到 `0/FFT#1` 的底层机制——查询串可以省略 device 或 counter。

字符串合法性由正则把关（定义在 constants.hpp）：

```text
VALID_BLOCKNAME_REGEX = [A-Za-z][A-Za-z0-9_]*        // 块名：字母开头，仅字母数字下划线
VALID_BLOCKID_REGEX   = (/DEVICE)? BLOCKNAME (#COUNTER)?
```

也就是说 `FIR#Filter` 非法（含 `#`），而 `0/Filter#1`、`FFT`（最简形式）都合法。匹配用的 `MATCH_BLOCKID_REGEX` 更宽松，允许 block_name 缺省。

#### 4.2.3 源码精读

**类文档与三段含义**，见 [block_id.hpp:21-38](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/block_id.hpp#L21-L38)。注释里把 `DEVICE/BLOCKNAME#COUNTER` 规则讲得很清楚，并明确「`0/FFT#1` means the second block called FFT on the first device」。

**三个私有成员**，对应三段：

```cpp
size_t _device_no;      // 设备号
std::string _block_name; // 块名
size_t _block_ctr;      // 同名块计数
```

见 [block_id.hpp:221-224](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/block_id.hpp#L221-L224)。

**字符串拼接**——`to_string()` 拼「设备/本地」，`get_local()` 拼「块名#计数」：

```cpp
std::string block_id_t::to_string() const {
    return str(boost::format("%d/%s") % get_device_no() % get_local());   // 如 "0/FFT#1"
}
std::string block_id_t::get_local() const {
    return str(boost::format("%s#%d") % get_block_name() % get_block_count()); // 如 "FFT#1"
}
```

见 [block_id.cpp:50-58](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_id.cpp#L50-L58)。注意即便计数是 0，`get_local()` 也会输出 `FFT#0`（始终带 `#N`）。

**属性树根路径**——`get_tree_root()` 把 block_id 转成属性树里的路径：

```cpp
uhd::fs_path block_id_t::get_tree_root() const {
    return uhd::fs_path("/blocks") / to_string();   // 如 "/blocks/0/FFT#1"
}
```

见 [block_id.cpp:60-63](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_id.cpp#L60-L63)。这承接了 u2-l4 的属性树模型：每个块在属性树里有自己的根 `/blocks/<block_id>/`，块的寄存器、属性都挂在这个根下。

**字符串解析 `set()`**——用正则 `VALID_BLOCKID_REGEX` 捕获三组，逐组回填：

```cpp
bool block_id_t::set(const std::string& new_name) {
    std::cmatch matches;
    if (not std::regex_match(new_name.c_str(), matches, std::regex(VALID_BLOCKID_REGEX))) {
        return false;            // 不合法直接返回 false，不抛异常
    }
    if (not(matches[1] == "")) { _device_no = uhd::cast::from_str<size_t>(matches[1]); }
    if (not(matches[2] == "")) { _block_name = matches[2]; }
    if (not(matches[3] == "")) { _block_ctr   = uhd::cast::from_str<size_t>(matches[3]); }
    return true;
}
```

见 [block_id.cpp:84-101](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_id.cpp#L84-L101)。要点：缺省的段（如 `"FFT"` 没有 device 和 counter）**保持原值不变**，这支持「增量式」设置。

**模糊匹配 `match()`**——比 `==` 更宽松：

```cpp
bool block_id_t::match(const std::string& block_str) {
    std::cmatch matches;
    if (not std::regex_match(block_str.c_str(), matches, std::regex(MATCH_BLOCKID_REGEX)))
        return false;
    return (matches[1] == "" or ... == _device_no)
       and (matches[2] == "" or matches[2] == _block_name)
       and (matches[3] == "" or ... == _block_ctr)
       and not(matches[1] == "" and matches[2] == "" and matches[3] == "");
}
```

见 [block_id.cpp:65-82](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_id.cpp#L65-L82)。语义：查询串里**缺省的段视为通配**。所以 `"FFT"` 匹配任意设备上的任意 FFT 块；`"FFT#1"` 只匹配 counter=1 的 FFT。最后一个 `and not(...)` 排除「三段全空」的退化情况。

**校验函数**：`is_valid_blockname` 只校验块名（不含 `/` 和 `#`），`is_valid_block_id` 校验整个 ID。见 [block_id.hpp:54-77](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/block_id.hpp#L54-L77) 与实现 [block_id.cpp:40-48](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_id.cpp#L40-L48)。

**运算符重载**：`block_id_t` 重载了 `==`、`!=`、`<`、`>`（按 device→name→ctr 字典序比较）、`++`（自增 counter）、与 `std::string`/`const char*` 的比较与赋值、以及到 `std::string` 的隐式转换。见 [block_id.hpp:147-219](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/block_id.hpp#L147-L219)。其中 `++` 让框架能方便地「递增 counter」给同名块编号。

#### 4.2.4 代码实践

**实践目标：** 验证 block_id 字符串规则与 `match` 的「缺省即通配」语义。

**操作步骤（源码阅读型）：**

1. 打开 [host/tests/block_id_test.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/block_id_test.cpp)（这个测试**在**编译清单里，见 `host/tests/CMakeLists.txt` 第 67 行 `block_id_test.cpp`）。
2. 找出测试里对 `to_string()`、`match()`、`set()` 的断言，对照上面讲的规则。

**可选的最小调用示例（示例代码，需链接 libuhd，结果待本地验证）：**

```cpp
// 示例代码：演示 block_id_t 的构造、拼接与匹配
#include <uhd/rfnoc/block_id.hpp>
using uhd::rfnoc::block_id_t;

int main() {
    block_id_t b("0/FFT#1");
    b.get();           // -> "0/FFT#1"
    b.get_local();     // -> "FFT#1"
    b.get_device_no(); // -> 0
    b.get_block_name();// -> "FFT"
    b.get_block_count();// -> 1

    block_id_t target("1/FFT#2");
    target.match("FFT");     // true  （缺省 device 和 counter → 通配）
    target.match("FFT#2");   // true
    target.match("0/FFT#2"); // false （device 不匹配）
    target.match("FFT#1");   // false （counter 不匹配）
}
```

**预期结果：** `match("FFT")` 对任何 FFT 块都返回 true；`to_string()` 始终输出完整的 `DEVICE/NAME#COUNTER` 三段。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1：** 写出「第 1 号设备上的第 3 个 DDC 块」的 block_id 字符串（counter 从 0 起）。

**参考答案：** `1/DDC#2`。设备号 1，块名 DDC，counter 从 0 计数所以第 3 个是 2。

**练习 2：** `block_id_t("FFT#1")` 和 `block_id_t("0/FFT#1")` 构造出来的对象，`get_device_no()` 分别是多少？为什么？

**参考答案：** 前者 device_no 保持默认值 0（默认构造函数设 `_device_no=0`，见 [block_id.cpp:20](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_id.cpp#L20-L20)；而 `set("FFT#1")` 中 device 段缺省，不修改原值）；后者显式为 0。两者 `to_string()` 都输出 `0/FFT#1`。

---

### 4.3 blockdef 描述

#### 4.3.1 概念说明

`blockdef` 想解决的问题是：给一个块写一份「说明书」，描述它有几个输入/输出端口、每个端口的数据格式、有哪些可配置参数（args）、有哪些寄存器。这样工具链（如 GNU Radio 的 RFNoC 模块、FPGA 构建脚本）不用读 C++ 源码就能知道一个块长什么样。

`blockdef` 是一个**抽象接口类**（[blockdef.hpp:21](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/blockdef.hpp#L21-L21)），它定义了访问块说明书的 API：`get_input_ports()`、`get_args()`、`get_settings_registers()` 等。它还提供一个静态工厂 `make_from_noc_id(noc_id)`，意图是「给定 NoC ID，去磁盘上找对应的描述符文件并解析」。

⚠️ **重要事实（容易踩坑）：** 在现代 UHD 主机驱动里，这条「描述符文件」路径**实际上没有接通**。后面 4.4 节会看到，主机的块实例化完全走「直接注册表」，而 `blockdef::make_from_noc_id` 在 `host/lib` 下**没有实现文件**（`blockdef.cpp` 不存在），对应的 `blockdef_test.cpp` 也**不在**编译清单里（编译的只有 `block_id_test.cpp`）。所以 `blockdef.hpp` 是一个**保留的公共头/历史接口**——它描述了一种类设计，但当前主机运行时并不依赖它。理解这一点能避免你误以为块实例化靠读 YAML 完成。

那 `host/include/uhd/rfnoc/blocks/*.yml` 这些 YAML 是什么？它们是 **RFNoC 工具链**（rfnoc_modtool、FPGA 构建脚本）用的块描述，schema 是 `rfnoc_modtool_args`，用来生成 FPGA 工程和 OOT 块脚手架——**不是** `blockdef` 类在运行时读的文件。但它们同样包含 `noc_id` 字段，因而和块的 C++ 注册、block_id 三者之间存在**对应关系**（这正是综合实践要梳理的）。

#### 4.3.2 核心流程

`blockdef` 抽象接口定义的数据模型：

```text
blockdef（一份块说明书）
  ├── get_key()           → 注册用的 block key（字符串）
  ├── get_name()          → 块名
  ├── noc_id()            → 对应的 NoC ID
  ├── is_block() / is_component()  → 是「块」还是「组件」
  ├── get_input_ports()   → ports_t（端口列表）
  ├── get_output_ports()  → ports_t
  ├── get_args()          → args_t（参数列表）
  ├── get_settings_registers()  → 寄存器名→地址
  └── get_readback_registers()  → 读回寄存器名→地址
```

其中 `port_t` 和 `arg_t` 都是 `uhd::dict<string,string>` 的派生——也就是说端口和参数都是「键值对集合」，值可以是变量（`$fftlen`）或关键字（`%vlen`）。

#### 4.3.3 源码精读

**抽象接口与静态工厂：**

```cpp
class UHD_API blockdef : public std::enable_shared_from_this<blockdef> {
public:
    typedef std::shared_ptr<blockdef> sptr;
    static sptr make_from_noc_id(uint64_t noc_id);   // 行 78：声明，但主机无实现
    virtual bool is_block() const = 0;
    virtual std::string get_key() const = 0;
    virtual std::string get_name() const = 0;
    virtual uint64_t noc_id() const = 0;
    virtual ports_t get_input_ports()  = 0;
    virtual ports_t get_output_ports() = 0;
    virtual args_t get_args() = 0;
    virtual registers_t get_settings_registers()  = 0;
    virtual registers_t get_readback_registers()  = 0;
    // ...
};
```

见 [blockdef.hpp:72-110](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/blockdef.hpp#L72-L110)。注意 `make_from_noc_id` 的参数是 `uint64_t`（64 位），而主机运行时块用的 `noc_id_t` 是 32 位（见 4.4 节）——这进一步说明 `blockdef` 是较早期的、与当前运行时机制脱节的设计。

**port_t 与 arg_t**：都是字典派生类，带 `is_variable`/`is_keyword`/`is_valid` 校验方法。见 [blockdef.hpp:33-68](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/blockdef.hpp#L33-L68)。

**真实的块描述（YAML，工具链用）：** 以 DDC 为例，[ddc.yml](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/blocks/ddc.yml) 的关键字段：

```yaml
schema: rfnoc_modtool_args        # ← 工具链描述，非运行时
module_name: ddc
name: Digital Downconverter
noc_id: 0xDDC00000                 # ← 与 C++ 注册宏的 NOC_ID 一致
parameters:
  NUM_PORTS: 1
data:
  inputs:
    in: { num_ports: NUM_PORTS, item_width: 32, format: sc16 }
  outputs:
    out: { num_ports: NUM_PORTS, item_width: 32, format: sc16 }
```

见 [ddc.yml:1-55](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/blocks/ddc.yml#L1-L55)。`noc_id: 0xDDC00000` 这一行是关键——它和 4.4 节看到的 C++ 宏 `UHD_RFNOC_BLOCK_REGISTER_DIRECT(..., 0xDDC00000, "DDC", ...)` 的第二个参数**完全一致**。YAML 描述的 `format: sc16`（每样本复 16 位整数）也与运行时该块的 otw 格式对应。

#### 4.3.4 代码实践

**实践目标：** 确认 `blockdef::make_from_noc_id` 在当前主机驱动中是否真的被使用。

**操作步骤：**

1. 在 `host/lib/` 下搜索 `make_from_noc_id` 的实现（用 `Grep` 搜 `blockdef` 目录或 `.cpp`）。
2. 在 `host/tests/CMakeLists.txt` 里搜 `blockdef`，确认它是否进入编译。

**需要观察的现象：**

- `host/lib/` 下**没有** `blockdef.cpp`（`git ls-files | grep -i blockdef` 只返回 `blockdef.hpp` 和 `blockdef_test.cpp`）。
- `host/tests/CMakeLists.txt` 里**只有** `block_id_test.cpp`，**没有** `blockdef_test.cpp`。

**预期结果：** 验证 `blockdef` 描述符机制在当前主机运行时是「保留接口、未启用」状态，块的端口/参数描述主要靠工具链 YAML 和 C++ 源码各自维护。

> 说明：本实践为源码阅读型，目的是建立准确认知，避免误判块实例化流程。

#### 4.3.5 小练习与答案

**练习 1：** 既然 `blockdef` 在主机运行时未启用，那框架在造块控制器时从哪里知道一个块有几个端口？

**参考答案：** 从 FPGA 的全局寄存器空间读。见 `noc_block_base::get_num_input_ports()` 的注释（[noc_block_base.hpp:85-95](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/noc_block_base.hpp#L85-L95)）：端口数「passed into this block from the information stored in the global register space」。也就是说端口数是**硬件在运行时告诉软件**的，而不是从描述文件读的。

**练习 2：** `ddc.yml` 里的 `noc_id: 0xDDC00000` 和 C++ 里的 `DDC_BLOCK` 常量是什么关系？

**参考答案：** 它们是同一个 NoC ID 的两种书写形式。`defaults.hpp` 里定义 `static const noc_id_t DDC_BLOCK = 0xDDC00000;`（[defaults.hpp:79](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/defaults.hpp#L79-L79)），YAML 里直接写十六进制字面量。两者必须一致，否则工具链生成的 FPGA 工程与主机控制器会对不上号。

---

### 4.4 registry 工厂机制

#### 4.4.1 概念说明

这是本讲最核心的一节，回答「框架怎么知道某个 NoC ID 对应哪个 C++ 类」。

答案是**自注册 + 查表**，和 u2-l1 讲的 `device::register_device` 几乎是同一套模式：

1. 每个块在自己的 `.cpp` 文件里，用一个宏（`UHD_RFNOC_BLOCK_REGISTER_DIRECT` 等）声明一个 `UHD_STATIC_BLOCK`。
2. 进程启动、`main` 之前，这些静态块自动执行，把「NoC ID → 工厂函数」登记进一张全局表。
3. 运行时，`rfnoc_graph` 从 FPGA 读出每块硬件的 NoC ID，去表里查到对应的工厂函数，调用它造出块控制器对象。

`registry` 类就是这张表的访问入口。它管理**两张**表：

- **直接注册表（direct registry）**：键是 `(noc_id, device_id)` 二元组，值是 `block_factory_info_t`（含块名、是否要主板访问、时钟名、工厂函数）。这是**当前实际使用**的表。
- **描述符注册表（descriptor registry）**：键是 block_key 字符串，值是工厂函数。这是为 `blockdef` 描述符路径预留的，当前未接通（查表函数里有 `// FIXME TODO` 标注）。

#### 4.4.2 核心流程

**注册阶段（静态初始化期，`main` 之前）：**

```text
进程启动
   │
   ├─ ddc_block_control.cpp 的 UHD_STATIC_BLOCK 执行
   │      └─ registry::register_block_direct(0xDDC00000, ANY_DEVICE, "DDC", false, CLOCK_KEY_GRAPH, "bus_clk", &ddc_block_control_make)
   │             └─ 存入 direct registry[(0xDDC00000, 0xFFFF)] = block_factory_info_t{...}
   │
   ├─ magnesium_radio_control.cpp 的 UHD_STATIC_BLOCK 执行
   │      └─ registry::register_block_direct(RADIO_BLOCK, N300, "Radio", true, "radio_clk", "bus_clk", &magnesium_radio_control_make)
   │             └─ 存入 direct registry[(0x12AD1000, 0x1300)] = block_factory_info_t{...}
   │
   └─ ... 其它所有块各自注册 ...
```

**实例化阶段（运行时，`rfnoc_graph` 枚举块时）：**

```text
graph 从 FPGA 读到一块硬件的 noc_id=0xDDC00000, device_id=0x1300
   │
   ▼
factory::get_block_factory(0xDDC00000, 0x1300)   // 查表
   │  查表顺序：
   │   1. 精确 (0xDDC00000, 0x1300)？  → 未命中（DDC 注册的是 ANY_DEVICE）
   │   2. 回退 (0xDDC00000, ANY_DEVICE)？ → 命中！
   │   3. 再不行 → 回退 (DEFAULT_NOC_ID, ANY_DEVICE) 兜底
   ▼
取出 block_factory_info_t.factory_fn
   │
   ▼
factory_fn(make_args)  →  std::make_shared<ddc_block_control_impl>(make_args)
   │
   ▼
框架给该实例分配 block_id（如 0/DDC#0），加入 block_container
```

查表的三级回退是关键设计：先按「精确设备」查，找不到就按「设备无关（ANY_DEVICE）」查，再找不到用默认块（`DEFAULT_NOC_ID = 0xFFFFFFFF`）兜底——这样即便遇到未知块也不会崩溃，只是退化为一个通用块控制器。

#### 4.4.3 源码精读

**注册宏（最常遇到）：**

```cpp
#define UHD_RFNOC_BLOCK_REGISTER_FOR_DEVICE_DIRECT(                    \
    CLASS_NAME, NOC_ID, DEVICE_ID, BLOCK_NAME, MB_ACCESS, TB_CLOCK, CTRL_CLOCK) \
    uhd::rfnoc::noc_block_base::sptr CLASS_NAME##_make(                \
        uhd::rfnoc::noc_block_base::make_args_ptr make_args)           \
    {                                                                   \
        return std::make_shared<CLASS_NAME##_impl>(std::move(make_args)); \
    }                                                                   \
    UHD_STATIC_BLOCK(register_rfnoc_##CLASS_NAME)                       \
    {                                                                   \
        uhd::rfnoc::registry::register_block_direct(NOC_ID, DEVICE_ID,  \
            BLOCK_NAME, MB_ACCESS, TB_CLOCK, CTRL_CLOCK,                \
            &CLASS_NAME##_make);                                        \
    }
```

见 [registry.hpp:18-34](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/registry.hpp#L18-L34)。这个宏干两件事：① 定义工厂函数 `CLASS_NAME##_make`，它 `make_shared` 出 `CLASS_NAME##_impl`（真正的实现类，继承 `noc_block_base`）；② 用 `UHD_STATIC_BLOCK`（u2-l1 讲过的静态初始化块机制）在 `main` 前调用 `register_block_direct` 登记它。

**两个便捷包装宏：**

```cpp
// 设备无关块（绝大多数 DSP 块用这个）：
#define UHD_RFNOC_BLOCK_REGISTER_DIRECT(CLASS_NAME, NOC_ID, BLOCK_NAME, TB_CLOCK, CTRL_CLOCK) \
    UHD_RFNOC_BLOCK_REGISTER_FOR_DEVICE_DIRECT(                          \
        CLASS_NAME, NOC_ID, ANY_DEVICE, BLOCK_NAME, false, TB_CLOCK, CTRL_CLOCK)
```

见 [registry.hpp:36-44](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/registry.hpp#L36-L44)。`UHD_RFNOC_BLOCK_REGISTER_DIRECT` 把 `DEVICE_ID` 固定为 `ANY_DEVICE`、`MB_ACCESS` 固定为 `false`；`UHD_RFNOC_BLOCK_REGISTER_DIRECT_MB_ACCESS` 则把 `MB_ACCESS` 固定为 `true`。

**类型别名与设备 ID 编码：**

```cpp
using noc_id_t     = uint32_t;   // 块类型 ID
using device_type_t = uint16_t;  // 设备类型 ID
static const device_type_t ANY_DEVICE = 0xFFFF;  // 设备无关
static const device_type_t N300 = 0x1300;
static const device_type_t X300 = 0xA300;
static const device_type_t X400 = 0xA400;
// ...
```

见 [defaults.hpp:52-74](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/defaults.hpp#L52-L74)。`device_type_t` 是 16 位，编码规则是「高 4 位家族 + 低 12 位型号」：

\[ \text{device\_type\_t} = (\text{family} \ll 12) \,|\, \text{model} \]

其中家族代号：`E` 系列 = `0xE`，`N` 系列 = `0x1`，`X` 系列 = `0xA`。例如 `N300 = 0x1300`、`X400 = 0xA400`、`E320 = 0xE320`。`ANY_DEVICE = 0xFFFF` 表示「不限定设备」。

**两张注册表（实现）：**

```cpp
// 直接注册表：键 = (noc_id, device_id) 二元组
using block_direct_reg_t = std::unordered_map<block_device_pair_t,
    block_factory_info_t, boost::hash<block_device_pair_t>>;
UHD_SINGLETON_FCN(block_direct_reg_t, get_direct_block_registry);

// 描述符注册表：键 = block_key 字符串
using block_descriptor_reg_t =
    std::unordered_map<std::string /* block_key */, registry::factory_t>;
UHD_SINGLETON_FCN(block_descriptor_reg_t, get_descriptor_block_registry);
```

见 [registry_factory.cpp:26-43](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/registry_factory.cpp#L26-L43)。两表都是进程级单例（`UHD_SINGLETON_FCN`）。直接注册表的键是 `std::pair<noc_id_t, device_type_t>`，因为同一 NoC ID 在不同设备上可能有不同实现（如 Radio 块：N300、X300、X400 各有自己的 `radio_control_impl`）。

**`register_block_direct` 实现（含重复注册保护）：**

```cpp
void registry::register_block_direct(noc_id_t noc_id, device_type_t device_id, ...) {
    block_device_pair_t key{noc_id, device_id};
    if (get_direct_block_registry().count(key)) {
        std::cerr << "[REGISTRY] WARNING: Attempting to overwrite previously "
                     "registered RFNoC block with noc_id,device_id: ...";
        return;        // 重复注册：打警告并忽略，不覆盖
    }
    get_direct_block_registry().emplace(key, block_factory_info_t{
        block_name, mb_access, timebase_clock, ctrlport_clock, std::move(factory_fn)});
}
```

见 [registry_factory.cpp:51-73](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/registry_factory.cpp#L51-L73)。注意它用 `std::cerr` 而不是 `UHD_LOG_*`，因为这段代码可能在静态初始化阶段执行，日志系统还没就绪（注释 [registry_factory.cpp:46-50](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/registry_factory.cpp#L46-L50) 明说了这点）。

**查表逻辑 `factory::get_block_factory`（三级回退）：**

```cpp
block_factory_info_t factory::get_block_factory(noc_id_t noc_id, device_type_t device_id) {
    // First, check the descriptor registry
    // FIXME TODO                              ← 描述符路径未接通！

    block_device_pair_t key{noc_id, device_id};
    if (!get_direct_block_registry().count(key)) {
        key = block_device_pair_t(noc_id, ANY_DEVICE);   // 回退 1：设备无关
    }
    if (!get_direct_block_registry().count(key)) {
        UHD_LOG_WARNING("RFNOC::BLOCK_FACTORY", "Could not find block ...");
        key = block_device_pair_t(DEFAULT_NOC_ID, ANY_DEVICE);  // 回退 2：默认块兜底
    }
    return get_direct_block_registry().at(key);
}
```

见 [registry_factory.cpp:109-127](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/registry_factory.cpp#L109-L127)。开头的 `// FIXME TODO` 就是 4.3 节说的「描述符路径未接通」的铁证——查表函数压根没去查描述符注册表。

**`block_factory_info_t` 结构（查表返回值）：**

```cpp
struct block_factory_info_t {
    std::string block_name;        // → 成为 block_id 的 BLOCKNAME 段
    bool mb_access;                // 是否申请主板访问
    std::string timebase_clk;      // 时间基准时钟名
    std::string ctrlport_clk;      // 控制端口时钟名
    registry::factory_t factory_fn;// 工厂函数指针
};
```

见 [factory.hpp:15-22](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/rfnoc/factory.hpp#L15-L22)。注意第一个字段 `block_name`——它正是注册时传给宏的 `BLOCK_NAME` 参数，最终成为 block_id 的中间段。比如 DDC 注册时传 `"DDC"`，所以它的 block_id 是 `0/DDC#0`。**这就是 block_id、blockdef、registry 三者的连接点**：注册宏的 `BLOCK_NAME` 既进了注册表（→ block_id 的名字段），又（在工具链侧）对应 YAML 的 `module_name`/`name`。

**真实注册调用（DDC，设备无关块）：**

```cpp
UHD_RFNOC_BLOCK_REGISTER_DIRECT(
    ddc_block_control, 0xDDC00000, "DDC", CLOCK_KEY_GRAPH, "bus_clk")
```

见 [ddc_block_control.cpp:698-699](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/ddc_block_control.cpp#L698-L699)。展开后：CLASS_NAME=`ddc_block_control`、NOC_ID=`0xDDC00000`、DEVICE_ID=`ANY_DEVICE`、BLOCK_NAME=`"DDC"`、MB_ACCESS=`false`、TB_CLOCK=`CLOCK_KEY_GRAPH`（DDC 的时间基准来自图，见 defaults.hpp 的 `CLOCK_KEY_GRAPH="__graph__"`）、CTRL_CLOCK=`"bus_clk"`。

**真实注册调用（Radio，设备相关 + 需主板访问）：**

```cpp
// magnesium_radio_control.cpp:1382 —— N300 系列的 Radio
UHD_RFNOC_BLOCK_REGISTER_FOR_DEVICE_DIRECT(
    magnesium_radio_control, RADIO_BLOCK, N300, "Radio", true, "radio_clk", "bus_clk");
// e31x_radio_control_impl.cpp:196 —— E310
// rhodium_radio_control.cpp:753 —— N320
// x400_radio_control.cpp:963 —— X400
// x300_radio_control.cpp:2076 —— X300
```

对比 DDC，Radio 注册有三个不同：① 用 `_FOR_DEVICE_DIRECT`（指定具体 `DEVICE_ID`，如 `N300`）；② `MB_ACCESS=true`（Radio 要控制主板）；③ 每个设备族有**各自的** `xxx_radio_control_impl` 类。这解释了为什么直接注册表的键必须是 `(noc_id, device_id)` 二元组——同一个 `RADIO_BLOCK`（`0x12AD1000`）在不同设备上对应不同的 C++ 类。

**内置块 NoC ID 一览：** [defaults.hpp:77-95](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/defaults.hpp#L77-L95) 列出了所有内置块的 NoC ID 常量，如 `DDC_BLOCK=0xDDC00000`、`FFT_BLOCK_V2=0xFF700002`、`RADIO_BLOCK=0x12AD1000`、`REPLAY_BLOCK=0x4E91A000` 等。这些常量在 `.cpp` 里既被注册宏引用，也对应工具链 YAML 里的 `noc_id` 字段。

#### 4.4.4 代码实践

**实践目标：** 在源码里追踪「同一个块类型，三处对应」——YAML、NoC ID 常量、注册宏。

**操作步骤：**

1. 打开 [host/include/uhd/rfnoc/blocks/fft.yml](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/blocks/fft.yml)，记下 `noc_id` 字段值（`0xFF700002`）和 `module_name`（`fft`）。
2. 在 [defaults.hpp:80-81](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/defaults.hpp#L80-L81) 找到 `FFT_BLOCK_V2 = 0xFF700002`，确认与 YAML 一致。
3. 在 [host/lib/rfnoc/fft_block_control.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/fft_block_control.cpp) 末尾找到注册宏，确认 `NOC_ID` 与 `BLOCK_NAME`（应为 `"FFT"`）。
4. 用 `Grep` 搜索 `UHD_RFNOC_BLOCK_REGISTER` 在 `host/lib/rfnoc/*.cpp` 中的出现次数（应为 19 个文件，每个块一个）。

**需要观察的现象：**

- YAML 的 `noc_id` ↔ `defaults.hpp` 的常量 ↔ 注册宏的 `NOC_ID`，三处是**同一个十六进制数**。
- 注册宏的 `BLOCK_NAME`（如 `"FFT"`）↔ YAML 的 `module_name`（如 `fft`，大小写差异），二者同名不同形。
- 这个 `BLOCK_NAME` 最终会出现在 block_id 里，如 `0/FFT#0`。

**预期结果：** 你能填出下面这张对应表（以 FFT 为例）：

| 出处 | 值 | 含义 |
| --- | --- | --- |
| fft.yml `noc_id` | `0xFF700002` | 块类型 ID（工具链侧） |
| defaults.hpp `FFT_BLOCK_V2` | `0xFF700002` | 块类型 ID（C++ 常量） |
| 注册宏 `NOC_ID` | `0xFF700002`（或常量） | 注册进直接注册表的键之一 |
| 注册宏 `BLOCK_NAME` | `"FFT"` | → block_id 的名字段 → `0/FFT#0` |

> 说明：本实践为源码阅读型，无需硬件。`Grep` 统计结果（19 个文件）可在本地复现验证。

#### 4.4.5 小练习与答案

**练习 1：** 为什么直接注册表的键是 `(noc_id, device_id)` 二元组，而不是只用 `noc_id`？

**参考答案：** 因为同一个 NoC ID 在不同设备族上可能对应**不同的 C++ 实现类**。最典型的就是 Radio 块（`RADIO_BLOCK=0x12AD1000`）：N300 用 `magnesium_radio_control`，X300 用 `x300_radio_control`，X400 用 `x400_radio_control`，各自的寄存器布局、子板管理逻辑都不同。用二元组 `(noc_id, device_id)` 作键，既能精确匹配到设备专属实现，又能通过 `(noc_id, ANY_DEVICE)` 回退到设备无关的实现（如 DDC 这类通用 DSP 块）。

**练习 2：** 如果 FPGA 上出现一个主机注册表里没有的全新块（未知 NoC ID），`get_block_factory` 会怎样？

**参考答案：** 走三级回退：精确查不到 → `(noc_id, ANY_DEVICE)` 也查不到 → 打一条 `UHD_LOG_WARNING` 告警，然后回退到 `(DEFAULT_NOC_ID, ANY_DEVICE)`，返回默认块（`DEFAULT_NOC_ID=0xFFFFFFFF`，块名默认 `"Block"`，见 [defaults.hpp:43-46](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/defaults.hpp#L43-L46)）。也就是退化为一个通用块控制器，不会让程序崩溃，但该块的专属功能不可用。

**练习 3：** DDC 的注册宏里 `TB_CLOCK` 是 `CLOCK_KEY_GRAPH`，而 Radio 的是 `"radio_clk"`，这说明什么？

**参考答案：** `TB_CLOCK`（timebase clock）指定块的时间基准从哪来。`CLOCK_KEY_GRAPH`（值为 `"__graph__"`，[defaults.hpp:16](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/defaults.hpp#L16-L16)）表示该块的时间基准**从流图连接推导**（DDC/DUC 这类纯 DSP 块自己没有独立时钟，时间从上游 Radio 传过来）。而 Radio 块有自己独立的射频时钟 `"radio_clk"`。这反映了「源头块有独立时钟、中间块继承上游时钟」的设计。

---

## 5. 综合实践

**任务：** 把本讲四个模块串起来，绘制一张「RFNoC 块从注册到寻址」的全链路追踪图，并以 FFT 块为具体例子填入真实值。

**要求：**

1. **注册侧**：画出进程启动期，`fft_block_control.cpp` 的 `UHD_STATIC_BLOCK` 如何调用 `registry::register_block_direct`，把 `(FFT_BLOCK_V2=0xFF700002, ANY_DEVICE)` → `block_factory_info_t{block_name="FFT", ...}` 存入直接注册表。标注涉及的文件：[registry.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/registry.hpp)、[registry_factory.cpp:51-73](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/registry_factory.cpp#L51-L73)。

2. **实例化侧**：画出运行时 `rfnoc_graph` 从 FPGA 读到 `noc_id=0xFF700002` 后，调用 [factory::get_block_factory](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/registry_factory.cpp#L109-L127) 查表（标注三级回退路径），取出 `factory_fn` 造出 `fft_block_control_impl`（继承 `noc_block_base`），最后框架分配 block_id `0/FFT#0`。

3. **寻址侧**：画出用户调用 `graph->get_block<fft_block_control>("FFT")` 时，[block_id_t::match("FFT")](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_id.cpp#L65-L82) 如何用「缺省即通配」规则命中 `0/FFT#0`。

4. **描述侧**：在图旁标注 [fft.yml](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/blocks/fft.yml) 的 `noc_id: 0xFF700002` 与上述 C++ 侧的对应关系，并注明它属于工具链描述、非主机运行时路径。

**产出：** 一张含「注册表（静态期）→ 查表实例化（运行期）→ block_id 寻址」三栏的流程图，FFT 的真实值（NoC ID、块名、block_id）填在对应节点上。完成后，你应能用一句话说清：「为什么 `get_block("FFT")` 能找到正确的 C++ 对象」。

> 说明：本实践为源码阅读 + 文档绘制型，无需硬件或编译。

---

## 6. 本讲小结

- **`noc_block_base`** 是所有块控制器的基类，双继承 `node_t`（属性系统）与 `register_iface_holder`（寄存器访问），统一管理块的身份（`_noc_id`、`_block_id`）、端口数、MTU 策略与属性子树；构造函数是 `protected`，只能由框架工厂调用。
- **`block_id_t`** 是块实例的三段式软件寻址 `DEVICE/BLOCKNAME#COUNTER`，靠正则校验合法性，靠 `match()` 实现「缺省即通配」的模糊匹配，并能把自身转成属性树路径 `/blocks/<block_id>/`。
- **`blockdef`** 定义了块说明书（端口/参数/寄存器）的抽象接口，但其在现代主机运行时**未启用**——`make_from_noc_id` 无实现、`blockdef_test` 不编译；块的端口数实际来自 FPGA 全局寄存器空间，块描述主要靠工具链 YAML 维护。
- **`registry`** 用自注册模式把「`(noc_id, device_id)` → 工厂函数」在 `main` 之前登记进直接注册表单例；运行时 `factory::get_block_factory` 经三级回退（精确设备 → ANY_DEVICE → DEFAULT_NOC_ID）查表造块。
- **三者连接点**是注册宏的 `BLOCK_NAME` 参数：它进了注册表成为 `block_factory_info_t.block_name`，最终构成 block_id 的名字段；同一 NoC ID 同时出现在 `defaults.hpp` 常量、`.cpp` 注册宏、工具链 YAML 三处，必须保持一致。
- **设备相关 vs 设备无关**：DDC 等通用 DSP 块用 `UHD_RFNOC_BLOCK_REGISTER_DIRECT`（`ANY_DEVICE`、无主板访问）；Radio 块按设备族各注册一个实现（指定 `DEVICE_ID`、`MB_ACCESS=true`），因此注册表键必须是 `(noc_id, device_id)` 二元组。

---

## 7. 下一步学习建议

下一讲 **u3-l3 流图连接与 commit** 将进入 `rfnoc_graph::connect` / `commit`，讲块与块、块与流器如何连成图、commit 时如何触发校验与属性传播。届时你会用到本讲的 block_id 来指定「连哪个块」，也会看到 `noc_block_base` 的属性系统如何在 commit 后被驱动。建议阅读：

- [host/lib/rfnoc/graph.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp)：`connect` 的多种重载与 `commit` 实现。
- [host/lib/rfnoc/block_container.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_container.cpp)：本讲提到的「block_container」如何按 block_id 存取块控制器。

如果想深入属性传播，可提前预读 u3-l5（experts 框架），届时 `noc_block_base` 继承的 `node_t` 会是主角。
