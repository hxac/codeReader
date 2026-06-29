# 设备地址 device_addr_t 与传输类型

## 1. 本讲目标

本讲承接 u2-l1 的「设备发现与工厂模式」，把镜头拉近到 `device::find` / `device::make` 最核心的一个数据结构——**设备地址 `device_addr_t`**。`device_addr_t` 既是用户告诉 UHD「我想找什么样的设备」的**提示（hint）**，也是设备被发现后回填的**身份档案**，还承担着向传输层传递运行参数（缓冲区大小等）的职责。

学完本讲，读者应该能够：

- 理解 `device_addr_t` 作为「键值对地址容器」的设计与三种构造方式，能写出合法的 args 字符串。
- 掌握 `device_filter_t` 三种过滤模式（ANY / USRP / CLOCK）在 `find`、`make`、`register_device` 中的作用。
- 理解设备哈希 `hash_device_addr` 如何实现**去重**和**设备复用**，以及为什么它要排序键、黑名单某些键。
- 能够解释 `type`、`addr`、`serial`、`resource`、`recv_buff_size` 等键各自如何影响 `find` 的结果。

---

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（来自 u1、u2-l1）：

- **UHD 设备抽象**：`uhd::device` 是所有硬件的抽象基类，`device::find` 只发现设备、不打开；`device::make` 才真正打开硬件返回对象。
- **设备自注册**：各设备实现通过 `UHD_STATIC_BLOCK` 在 `main` 之前把 `(find, make, filter)` 三元组压入全局注册表（u2-l1 已讲）。
- **dict 容器**：UHD 自己实现的 `uhd::dict<Key, Val>`，一个「类 Python 接口」的有序字典，提供 `has_key / get / set / pop / keys / vals` 等方法。

本讲会反复用到这几个直觉：

1. **同一个 `device_addr_t` 在不同阶段语义不同**：作为入参时是「提示」，作为 `find` 返回值时是「档案」，附加 `recv_buff_size` 等键时又变成「传输层参数」。容器不变，含义随上下文变化。
2. **键名是约定，不是强类型**：`device_addr_t` 本质是 `dict<string,string>`，没有任何字段约束。`type`、`addr`、`serial` 这些键名是 UHD 各设备实现共同遵守的**约定**，由过滤逻辑和哈希逻辑隐式定义。
3. **过滤 + 去重是两道独立的工序**：`find` 先用 `name/serial/type/product` 四个键过滤候选，再用设备哈希去重，两者不能混淆。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `host/include/uhd/types/device_addr.hpp` | `device_addr_t` 类声明，继承自 `dict<string,string>`，定义 args 字符串接口与多设备拆分/合并函数。 |
| `host/lib/types/device_addr.cpp` | args 字符串解析（`,` 与 `=` 分隔）、`to_string`/`to_pp_string`，以及 `separate_device_addr` / `combine_device_addrs` 多设备索引处理。 |
| `host/include/uhd/device.hpp` | 声明 `device_filter_t { ANY, USRP, CLOCK }` 枚举，以及带 `filter` 参数的 `find` / `make` / `register_device`。 |
| `host/lib/device.cpp` | `hash_device_addr` 哈希函数、基于 `name/serial/type/product` 的过滤、按哈希去重、以及 `make` 中的哈希→`weak_ptr` 设备缓存。 |
| `host/lib/utils/serial_number.cpp` | `serial_numbers_match`：把序列号当十六进制整数比较，实现容错匹配。 |
| `host/lib/utils/prefs.cpp` | `prefs::get_usrp_args`：在 `make` 阶段把配置文件里的 `type/product/serial` 段合并进设备地址。 |

---

## 4. 核心概念与源码讲解

### 4.1 device_addr_t：键值对设备地址容器

#### 4.1.1 概念说明

`device_addr_t` 是 UHD 用来「定位设备」的核心数据结构。它的头文件文档注释把它说得很清楚：

> Mapping of key/value pairs for locating devices on the system. When left empty, the device discovery routines will search all available transports on the system (ethernet, usb...).

也就是说：

- **留空**时，`find` 会扫描主机上所有传输方式（以太网、USB……），把能回应的设备都列出来。
- **填入键值对**时，相当于给发现过程加约束，缩小到特定设备。比如填 `addr=192.168.10.2` 就只找那个 IP 上的 USRP。
- 它还能**携带传输层参数**，例如 `recv_buff_size=1e6` 调整接收缓冲区。

关键设计取舍：UHD 没有为「网络设备」「USB 设备」分别定义强类型结构，而是统一用一个 `dict<string,string>`。这样做的好处是设备发现逻辑与具体传输解耦，坏处是键名靠约定，初学者需要记住哪些键有效。

`device_addr_t` 继承自 `uhd::dict<std::string, std::string>`，所以它天然拥有 `dict` 的全部方法（`has_key`、`get`、`set`、`pop`、`keys`、`[]` 运算符等），自己只额外加了字符串解析、格式化和类型转换能力。

#### 4.1.2 核心流程

一个 `device_addr_t` 的「一生」可以概括为三态：

1. **构造态**：用户用 args 字符串、`std::map` 或逐个 `set` 构造它，作为 hint 传入 `find`/`make`。
   - args 字符串格式：`key1=value1,key2=value2`，用 `,` 分隔键值对，用 `=` 分隔键与值。
2. **档案态**：设备实现层的 `find` 函数被调用时，把探测到的设备信息（`type`、`addr`、`serial`、`product`……）填进一个新的 `device_addr_t` 返回。
3. **参数态**：`make` 阶段，用户 hint 里不在档案中的键会被回填进档案，作为传输层参数一并交给工厂函数。

构造时 args 字符串的解析规则（伪代码）：

```text
对每个用 "," 切出的 pair:
    如果 pair 经 trim 后为空 -> 跳过
    用 "=" 把 pair 切成 toks
    如果 toks 只有 1 个（没有 "="）-> 补一个空值 ""
    如果 toks 恰好 2 个 且 键名非空 -> set(trim(键), trim(值))
    否则 -> 抛 value_error("invalid args string")
```

注意「键没有值」是合法的：`recv_offload`（不带 `=`）会被解析成 `recv_offload -> ""`，这种「开关型」键常用于布尔标志。

#### 4.1.3 源码精读

**类声明：继承 dict，三种构造，两个序列化方法。**

[host/include/uhd/types/device_addr.hpp:38-45](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/device_addr.hpp#L38-L45) 这几行定义了 `device_addr_t` 继承 `dict<string,string>`，并提供从 args 字符串构造的默认构造函数。注意它有默认参数 `args = ""`，所以 `device_addr_t()` 构造的是空地址（让 `find` 扫描全部传输）。

[host/include/uhd/types/device_addr.hpp:54-57](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/device_addr.hpp#L54-L57) 提供从 `std::map<string,string>` 构造的重载，方便把别的来源（如 RPC 返回的字典）直接转成设备地址。

[host/include/uhd/types/device_addr.hpp:63-70](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/device_addr.hpp#L63-L70) 定义两个序列化方向：`to_pp_string()` 给人看（多行 `Device Address:` 格式），`to_string()` 给机器看（带分隔符的 args 串）。

[host/include/uhd/types/device_addr.hpp:80-90](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/device_addr.hpp#L80-L90) 是 `cast<T>(key, def)` 模板方法：取一个键的值并尝试转换成类型 `T`，键不存在时返回默认值 `def`，转换失败抛异常。这是设备实现层读取「带类型的可选参数」的标准姿势，例如读 `recv_buff_size` 成 `size_t`。

[host/include/uhd/types/device_addr.hpp:93-100](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/device_addr.hpp#L93-L100) 定义 `device_addrs_t`（地址的 vector，`find` 的返回类型），以及一对多设备函数 `separate_device_addr` / `combine_device_addrs`（见 4.1 节末尾的多设备部分）。

**args 字符串解析实现。**

[host/lib/types/device_addr.cpp:20-21](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/types/device_addr.cpp#L20-L21) 定义两个分隔符常量：`arg_delim = ","`（键值对之间）和 `pair_delim = "="`（键与值之间）。

[host/lib/types/device_addr.cpp:31-47](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/types/device_addr.cpp#L31-L47) 是字符串构造函数的核心。它用 boost 的 `char_separator` 分词，逐对处理。第 40-41 行「单 token 补空值」正是「不带 `=` 的开关键」能成立的原因；第 42-43 行只在「恰好 2 个 token 且键名非空」时才 `set`，并对键值都做了 `trim`（所以 `addr = 192.168.10.2` 这种带空格的写法也能解析）；其余情况第 45 行抛 `value_error`。

[host/lib/types/device_addr.cpp:74-82](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/types/device_addr.cpp#L74-L82) 是 `to_string` 的实现，是构造函数的逆运算：把每个键值对拼成 `key=value`，用 `,` 连接。第一个键前不加逗号（靠 `count` 计数判断）。

**多设备地址的拆分与合并。**

[host/lib/types/device_addr.cpp:86-139](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/types/device_addr.cpp#L86-L139) 实现 `separate_device_addr`：把一个「带数字后缀的多设备地址」拆成多个单设备地址。它的规则是用正则 `^(\D+\d*\D+)(\d*)$`（第 115-116 行）把键切成「名字部分 + 数字后缀」：
- 数字后缀表示主板索引，例如 `addr0`、`addr1` 分别对应第 0、1 块主板。
- 没有数字后缀的键（如 `type`）是「全局键」，会被复制到拆出的每一个设备地址里（第 131-137 行）。
- 第 89-106 行还兼容了一种**已废弃**的写法（`addr = "ip1 ip2"` 空格分隔多 IP）并打印 deprecation 警告，引导用户改用 `addr0/addr1` 写法。

> 关于那条正则：`(\D+\d*\D+)` 匹配「至少一个非数字、可选数字、再至少一个非数字」的名字部分，`(\d*)` 匹配末尾的索引数字。这样设计是为了**允许键名中间出现数字**（例如 `recv_offload_thread_0_cpu` 这种合法键不会被误判成「名字 `recv_offload_thread_` + 索引 `0_cpu`」）。

[host/lib/types/device_addr.cpp:141-150](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/types/device_addr.cpp#L141-L150) 是 `combine_device_addrs`，逆操作：把多个地址按索引拼回 `key0`、`key1` 形式。

#### 4.1.4 代码实践

**实践目标**：亲手构造几种 `device_addr_t`，理解键值对如何作为「提示」影响 `find` 的候选集。

**操作步骤**：

1. 阅读下面的「示例代码」（非项目原有代码，仅为说明用法）：

   ```cpp
   // 示例代码：演示 device_addr_t 的三种构造与作用
   #include <uhd/types/device_addr.hpp>
   using namespace uhd;

   // 方式 A：空地址 —— 让 find 扫描所有传输
   device_addr_t empty_hint;            // == device_addr_t("")

   // 方式 B：args 字符串 —— 限定到某个 IP 的设备
   device_addr_t net_hint("addr=192.168.10.2");

   // 方式 C：args 字符串 —— 带传输层参数
   device_addr_t tuned_hint("type=usrp,addr=192.168.10.2,recv_buff_size=1000000");

   // 方式 D：逐个 set
   device_addr_t manual;
   manual["type"]   = "usrp";
   manual["serial"] = "F12345";
   ```

2. 打开 [host/lib/usrp/mpmd/mpmd_find.cpp:100-119](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_find.cpp#L100-L119)，观察现代设备（N/X 系列）被探测到后，`find` 函数往 `new_addr` 里填了哪些键：`addr`（管理地址）、`type`（初始为 `"mpmd"`，后续被硬件描述覆盖）、以及从发现广播包里解析出的一批键值对（含 `serial`、`product` 等）。这就是「档案态」的真实样子。

3. 思考并回答：如果用户的 hint 写成 `type=usrp`，而一台设备档案里 `type` 是 `mpmd`，过滤会发生什么？（提示：见 4.2 节与 4.3 节的过滤逻辑，注意 `sim` 特例。）

**需要观察的现象**：`device_addr_t` 是「无类型约束的键值容器」，键名是否生效完全取决于下游过滤逻辑是否读取它；`recv_buff_size` 这类键不会被过滤逻辑读取，因此不影响「能不能找到」，但会影响 `make` 后的传输行为。

**预期结果**：能用自己的话说清「`addr`/`serial`/`type` 影响 find 的命中集合，`recv_buff_size`/`send_buff_size` 只影响传输参数」这一区别。**待本地验证**：在有硬件的环境下运行 `uhd_find_devices --args "addr=192.168.10.2"` 与不带 args 两次，对比输出。

#### 4.1.5 小练习与答案

**练习 1**：下面三个 args 字符串分别会被解析成什么键值对？

- (a) `"addr=192.168.10.2,recv_buff_size=1e6"`
- (b) `"recv_offload"`
- (c) `"=value"`（只有值没有键）

> **答案**：
> - (a) 两个键：`addr -> "192.168.10.2"`、`recv_buff_size -> "1e6"`。
> - (b) 一个键：`recv_offload -> ""`（单 token 补空值，充当开关）。
> - (c) 抛 `uhd::value_error("invalid args string: =value")`，因为键名 trim 后为空，不满足「键名非空」条件（见 device_addr.cpp 第 42 行）。

**练习 2**：为什么 `separate_device_addr` 用正则区分「名字部分」和「索引后缀」，而不是简单地取键名末尾的数字？

> **答案**：因为有些合法键名本身中间就含数字，例如 `recv_offload_thread_0_cpu`。简单取末尾数字会把它误判成「名字 `recv_offload_thread_` + 索引 `0_cpu`」，而 `0_cpu` 不是合法索引。正则 `(\D+\d*\D+)(\d*)` 要求索引后缀前必须先有一段非数字结尾的名字，从而避免误判。

---

### 4.2 device_filter_t：设备类型过滤

#### 4.2.1 概念说明

UHD 不止管理 USRP 射频设备，还管理一些「周边设备」，最典型的是 **OctoClock**（一种 GPSDO/时钟分发器，属于 `CLOCK` 类型）。`device_filter_t` 就是一个三值枚举，让调用者声明「我这次只想找/造哪一类设备」，避免把时钟设备和射频设备混在一起返回。

三种取值：

| 取值 | 含义 |
| --- | --- |
| `ANY` | 不过滤，USRP 和 CLOCK 都参与。 |
| `USRP` | 只要射频设备（绝大多数应用场景）。 |
| `CLOCK` | 只要时钟设备（如 OctoClock）。 |

注意「过滤」发生在**两个层面**：

1. **注册层面**：每个设备实现注册时自带一个 `filter` 标签（u2-l1 讲过的三元组里的第三个值）。
2. **调用层面**：`find`/`make` 的调用者也传一个 `filter`。只有「调用者要的类型」与「实现声明的类型」匹配，该实现的 `find` 函数才会被调用。

#### 4.2.2 核心流程

过滤的匹配规则很简单（伪代码）：

```text
对注册表里每一个 (find, make, impl_filter) 三元组:
    如果 调用者 filter == ANY  或者  impl_filter == 调用者 filter:
        调用 find(hint)，把结果加入候选
    否则:
        跳过该实现
```

也就是说：调用者传 `ANY` 表示「我全都要」；调用者传 `USRP` 表示「我只要那些自己声明是 USRP 的实现」。这能保证比如 `uhd_usrp_probe`（只关心射频设备）不会误把 OctoClock 当成 USRP 打开。

#### 4.2.3 源码精读

**枚举定义。**

[host/include/uhd/device.hpp:33-34](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L33-L34) 定义枚举 `device_filter_t { ANY, USRP, CLOCK }`，注释说明它「used as a filter in make」（实际上 `find` 和 `register_device` 都用）。

**带 filter 的三件套签名。**

[host/include/uhd/device.hpp:44-45](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L44-L45) `register_device` 的第三参数就是 `filter`，设备实现自注册时声明自己是哪一类。

[host/include/uhd/device.hpp:57](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L57) `find` 的第二参数 `filter` 默认 `ANY`。

[host/include/uhd/device.hpp:73-74](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L73-L74) `make` 同样带 `filter`，默认 `ANY`。

**过滤匹配在发现循环里的落地。**

[host/lib/device.cpp:86-99](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L86-L99) `discover_devices_with_makers` 用 `std::async` 并发调用各实现的 `find`。第 94 行就是匹配判据：`if (filter == device::ANY or std::get<2>(fcn) == filter)` —— 要么调用者要全部，要么该实现的声明类型正好等于调用者要的类型，才会把这个实现的 `find` 任务投递出去。这正是上一节伪代码的实现。

[host/lib/device.cpp:117-163](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L117-L163) `find_filtered_devices_with_makers` 在拿到候选后，还做了**第二道过滤**：用 hint 里的 `name`/`serial`/`type`/`product` 四个键逐一匹配（第 131-138 行）。注意这里的 `type` 是 `device_addr_t` 里的 `type` 字段（字符串，如 `"usrp"`、`"mpmd"`、`"sim"`），和本节的枚举 `device_filter_t` 是**两回事**——前者是设备档案里的产品类型字符串，后者是粗粒度的设备大类。第 136 行 `or hint["type"] == "sim"` 是给 UHD 仿真器（`uhd::device` 的 sim 实现）开的特例：sim 设备档案里的 `type` 字段未必等于 `"sim"`，所以只要 hint 要 `type=sim` 就放行。

#### 4.2.4 代码实践

**实践目标**：理解「调用者 filter」与「实现声明 filter」如何共同决定哪些设备实现参与发现。

**操作步骤**：

1. 用只读 git 命令统计所有自注册设备实现各自声明的 `filter`：

   ```bash
   git grep -n "register_device" -- 'host/lib/**/*.cpp'
   ```

   对照每个调用点的第三个实参（`device::USRP` 或 `device::CLOCK`），数一数 USRP 实现和 CLOCK 实现各有多少个。（u2-l1 已给出总数：7 个 USRP + 1 个 CLOCK。）

2. 阅读 [host/lib/device.cpp:93-98](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L93-L98)，确认当调用者传 `USRP` 时，那个 `CLOCK` 实现的 `find` 不会被投递。

3. 思考：为什么 `uhd_usrp_probe` 这种工具应当传 `USRP` 而不是 `ANY`？（提示：probe 会接着 `device::make` 打开设备，如果误把 OctoClock 当 USRP 去 `get_rx_stream` 会怎样？）

**需要观察的现象**：`filter` 在「注册表遍历」阶段就生效，错误的实现根本不会被调用，而不是先调用再丢弃结果。

**预期结果**：能画出一张表，列出每个设备实现文件 → 其 `register_device` 的第三参数 → 调用者 `find(USRP)` 时它是否参与。**待本地验证**：无硬件时通过源码阅读完成；有 OctoClock 时可对比 `uhd_find_devices`（默认 `ANY`）与 `uhd_find_devices` 走 USRP-only 路径的输出差异。

#### 4.2.5 小练习与答案

**练习 1**：调用者 `find(hint, device::CLOCK)` 时，一个声明为 `device::USRP` 的实现会被调用几次？

> **答案**：0 次。匹配条件是 `filter == ANY or impl_filter == filter`，这里 `filter = CLOCK`、`impl_filter = USRP`，两个分支都不满足，第 94 行的 `if` 为假，该实现的 `find` 任务不会被投递。

**练习 2**：`device_filter_t` 枚举和 `device_addr_t["type"]` 字符串都叫「type」，它们是什么关系？

> **答案**：完全不同的两个概念。`device_filter_t` 是粗粒度的设备大类（ANY/USRP/CLOCK），在注册表遍历时用；`device_addr_t["type"]` 是设备档案里的产品类型字符串（如 `"usrp"`、`"mpmd"`、`"sim"`、`"usrp2"`），在 [host/lib/device.cpp:135](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L135) 的第二道过滤里用。一个设备可能 `device_filter_t == USRP` 而 `["type"] == "mpmd"`。

---

### 4.3 设备哈希与去重

#### 4.3.1 概念说明

设备哈希 `hash_device_addr` 是把一个 `device_addr_t` 压缩成一个 `size_t` 整数的函数，用于两个关键目的：

1. **`find` 阶段去重**：一个设备可能通过多条路径被发现（例如同时响应广播、被多次探测），`find` 不能把同一台设备返回多次。用哈希去重能合并这些重复条目。
2. **`make` 阶段设备复用**：同一台设备如果已经被打开过，再次 `make` 不应该创建第二个对象，而应返回已有的 `device` 句柄。哈希作为「设备身份」的键，映射到现有设备的 `weak_ptr`。

哈希函数要做到「**同一台设备 → 同一个哈希**」，即使两次发现的 `device_addr_t` 键值对顺序不同、或夹带了一些无关的临时键。这正是它要「排序键」和「黑名单某些键」的原因。

#### 4.3.2 核心流程

哈希计算流程（伪代码）：

```text
hash = 0
如果 dev_addr 有 "resource" 键:           # resource 优先，单独哈希
    hash_combine(hash, "resource")
    hash_combine(hash, dev_addr["resource"])
否则:
    对 dev_addr.keys() 排序后的每个 key:
        如果 key 在黑名单 {claimed, skip_dram, skip_ddc, skip_duc} 里: 跳过
        hash_combine(hash, key)
        hash_combine(hash, dev_addr[key])
返回 hash
```

`boost::hash_combine(seed, value)` 的标准实现是把当前种子与新值的哈希混合，经典配方为：

\[
\text{seed} \mathrel{\oplus}= \text{hash}(\text{value}) + 0\text{x}9e3779b9 + (\text{seed} \ll 6) + (\text{seed} \gg 2)
\]

其中 \(0\text{x}9e3779b9} \) 是黄金分割常量（\( 2^{32}/\phi \)），目的是让相邻种子尽量分散，避免哈希碰撞。本讲不必死记公式，只需记住「**每一对 (key, value) 都会被混合进种子**」。

去重流程（在 `find` 内）：

```text
seen = 空集合
对每个候选 (addr, make):
    h = hash_device_addr(addr)
    如果 h 在 seen 里: 删除该候选（重复）
    否则: 把 h 加入 seen
返回去重后的候选
```

设备复用流程（在 `make` 内）：

```text
h = hash_device_addr(选中的 addr)
如果 cache[h] 里的 weak_ptr 还活着: 返回那个已有设备
否则: 用 make 函数新建设备，cache[h] = 新设备的 weak_ptr，返回
```

#### 4.3.3 源码精读

**哈希函数本体。**

[host/lib/device.cpp:36-59](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L36-L59) 定义 `hash_device_addr`。第 43-44 行的黑名单 `{"claimed", "skip_dram", "skip_ddc", "skip_duc"}` 是关键：这些键是**运行期临时状态**（如 `claimed` 表示设备是否被占用、`skip_dram` 表示是否跳过 DRAM 初始化），不应该影响「设备身份」，所以排除。第 46-48 行对 `resource` 键做了特判：对于像 X4xx 这类通过 PCIe `resource` 路径寻址的设备，`resource` 是唯一稳定标识，所以只哈希它一个键（不再哈希其他键），避免其他随探测变化的键干扰身份。

[host/lib/device.cpp:50-56](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L50-L56) 是一般情况：用 `uhd::sorted(dev_addr.keys())` **先排序**再哈希。这一步保证了「键值对相同但插入顺序不同」的两个 `device_addr_t` 产生相同哈希——去重的前提。

**find 阶段的去重落地。**

[host/lib/device.cpp:148-160](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L148-L160) 用一个 `std::set<size_t> device_hashes` 配合 `std::remove_if` 做就地去重。第 155-156 行的逻辑：算出当前候选的哈希 `hash`，如果 `device_hashes` 里已存在（`count(hash)` 非 0）就返回 `true` 把它删掉，否则把它插入集合。这用的是「erase–remove 惯用法」。

**make 阶段的设备复用。**

[host/lib/device.cpp:216-217](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L216-L217) 计算选中设备的哈希 `dev_hash`，第 217 行用 trace 日志记录（调试设备复用问题时很有用）。

[host/lib/device.cpp:219-224](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L219-L224) 把「用户 hint 里有、但设备档案里没有」的键回填进档案。这正是 4.1 节说的「参数态」：像 `recv_buff_size` 这种传输参数由此进入设备地址，最终传给 `make` 函数。

[host/lib/device.cpp:227-241](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L227-L241) 是设备复用的核心：第 227 行声明静态字典 `hash_to_device`（哈希 → `weak_ptr<device>`）；第 230-234 行检查该哈希对应的 `weak_ptr` 是否还活着，活着就直接返回已有设备；否则第 239 行调用 `maker(prefs::get_usrp_args(dev_addr))` 新建并登记。注意第 239 行先经过 `prefs::get_usrp_args` 合并配置文件参数（见下方补充）。

**配套：序列号容错匹配。**

[host/lib/utils/serial_number.cpp:13-24](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/utils/serial_number.cpp#L13-L24) 的 `serial_numbers_match` 把两个序列号都按**十六进制整数** `std::stoi(..., 0, 16)` 解析再比较，而不是直接字符串比较。这样 `0F123` 和 `F123`（数值相同、前导零不同）会被视为同一设备。这是 4.2 节过滤阶段对 `serial` 键的匹配方式（[host/lib/device.cpp:132-134](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L132-L134) 调用它）。解析失败（非法字符或溢出）返回 `false`，不会抛异常中断发现。

**配套：配置文件参数合并。**

[host/lib/utils/prefs.cpp:149-153](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/utils/prefs.cpp#L149-L153) 的 `get_usrp_args` 在 `make` 前把 UHD 配置文件里以 `type`/`product`/`serial` 为索引的参数段合并进设备地址（第 151 行）。这意味着用户可以在配置文件里为某台设备（按序列号）预设一组传输参数，而不用每次命令行都写。

#### 4.3.4 代码实践

**实践目标**：亲手推演哈希去重，理解「为什么排序键 + 黑名单能保证同一设备哈希稳定」。

**操作步骤**：

1. 阅读下面的「示例代码」（非项目原有代码，仅模拟哈希计算）：

   ```cpp
   // 示例代码：模拟 hash_device_addr 的行为
   device_addr_t a;            // 第一次发现
   a["type"]   = "mpmd";
   a["addr"]   = "192.168.10.2";
   a["serial"] = "F12345";
   a["claimed"] = "false";     // 运行期临时键

   device_addr_t b;            // 第二次发现（顺序不同 + 带 skip_dram）
   b["serial"]   = "F12345";
   b["skip_dram"]= "1";        // 黑名单键
   b["addr"]     = "192.168.10.2";
   b["type"]     = "mpmd";
   ```

2. 推演 `hash_device_addr(a)` 与 `hash_device_addr(b)` 的过程：
   - 两者都没有 `resource` 键，走排序分支。
   - 排序后有效键都是 `addr / serial / type`（`claimed`、`skip_dram` 在黑名单里被剔除）。
   - 三对 `(key, value)` 完全相同，且都以相同顺序喂给 `hash_combine`。
   - 结论：**两者哈希相等，`find` 会把第二次出现的当作重复删掉**。

3. 用 git 命令确认黑名单四个键各自在代码里出现的位置与用途（例如 `skip_dram` 在哪里被设置）：

   ```bash
   git grep -n "skip_dram" -- 'host/lib/**/*.cpp' | head
   ```

**需要观察的现象**：插入顺序不同、或夹带黑名单临时键，都不改变哈希；但任意一个「有效键」的值变化（比如 `addr` 变了）会让哈希不同。

**预期结果**：能解释「为什么 `claimed=false` 不会让同一台设备在两次 `find` 之间产生不同哈希」。**待本地验证**：在有硬件的环境下连续两次 `uhd_find_devices`，确认同一设备只出现一次。

#### 4.3.5 小练习与答案

**练习 1**：两台序列号不同但型号、IP 都相同的设备，哈希会相同吗？

> **答案**：不会。`serial` 是有效键（不在黑名单里），参与哈希。序列号不同 ⇒ `hash_combine` 喂入的值不同 ⇒ 哈希不同 ⇒ 被当作两台设备。这正符合预期。

**练习 2**：为什么 `make` 里用 `weak_ptr` 而不是 `shared_ptr` 缓存设备？

> **答案**：用 `weak_ptr`（[host/lib/device.cpp:227](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L227)）观察设备生命周期，不强行延长它。如果用户已经释放了某台设备的所有 `shared_ptr`（设备真正销毁），`weak_ptr` 的 `lock()` 返回空，缓存条目自然失效，下次 `make` 会新建——这是正确的语义。若用 `shared_ptr` 缓存，设备将永不被销毁（缓存一直持有引用），造成资源泄漏。

**练习 3**：`hash_device_addr` 对 `resource` 键做了什么特判，为什么？

> **答案**：见 [host/lib/device.cpp:46-48](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L46-L48)。若设备地址含 `resource` 键，则**只**哈希 `resource` 这一个键，忽略其他所有键。因为对 PCIe 等以 `resource`（如 BAR 设备路径）寻址的设备，`resource` 是唯一稳定身份标识，而其他键可能随探测变化，只哈希 `resource` 才能保证同一设备哈希稳定。

---

## 5. 综合实践

**综合任务**：把本讲三个模块串起来，画出一条「从用户 args 字符串到设备对象」的完整数据流，并标注 `device_addr_t` 在每一步的形态变化。

**操作步骤**：

1. 假设用户执行 `multi_usrp::make("type=usrp,addr=192.168.10.2,recv_buff_size=1000000")`（`multi_usrp` 内部会调 `device::make`，详见下一讲 u2-l3）。请按顺序回答：
   - (a) args 字符串先被解析成 `device_addr_t` 的哪几个键？依据是哪段源码？
   - (b) `find` 阶段，`device_filter_t` 默认值是什么？哪几个设备实现的 `find` 会被调用？依据 [host/lib/device.cpp:93-98](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L93-L98)。
   - (c) 候选回来后，第二道过滤用 hint 的哪些键匹配？`recv_buff_size` 会被用来过滤吗？依据 [host/lib/device.cpp:131-138](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L131-L138)。
   - (d) 去重依据什么？如果同一 IP 的设备被探测到两次会怎样？依据 4.3 节。
   - (e) `make` 阶段，`recv_buff_size` 是怎么最终进入设备对象的？依据 [host/lib/device.cpp:219-224](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L219-L224) 与 [host/lib/device.cpp:239](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device.cpp#L239)。

2. 画一张图，横轴是「时间/调用阶段」（构造 → find 注册表过滤 → find 第二道键过滤 → find 去重 → make 选 which → make 回填 hint → make 哈希缓存 → maker 新建），纵轴标注 `device_addr_t` 在该阶段的角色（提示 / 档案 / 参数）。

**预期结果**：得到一张完整的数据流图，能清楚区分「哪些键影响能不能找到设备（type/serial/name/product/addr）、哪些键只影响传输参数（recv_buff_size 等）、哪些键不影响身份（claimed/skip_*）」。这张图也是阅读下一讲 `multi_usrp`（u2-l3）的必备前置。

---

## 6. 本讲小结

- `device_addr_t` 是继承自 `dict<string,string>` 的无类型键值容器，有 args 字符串、`std::map`、逐个 `set` 三种构造方式；args 字符串用 `,` 分隔键值对、`=` 分隔键与值，单 token 会被补空值充当开关键。
- 同一个 `device_addr_t` 在不同阶段有三种语义：入参时是「提示（hint）」、`find` 返回时是「档案」、附加 `recv_buff_size` 等键后是「传输层参数」。
- `device_filter_t { ANY, USRP, CLOCK }` 是粗粒度设备大类，在注册表遍历时过滤；它和 `device_addr_t["type"]`（产品类型字符串）是两个不同概念，后者在第二道过滤里用，并对 `sim` 开了特例。
- `find` 的两道工序是：先用各实现的 `find` + `filter` 枚举候选，再用 hint 的 `name/serial/type/product` 四键精确匹配，其中 `serial` 用十六进制数值比较（`serial_numbers_match`）容错。
- 设备哈希 `hash_device_addr` 通过「排序键 + 黑名单临时键（claimed/skip_*）+ resource 特判」保证「同一设备 → 同一哈希」，用于 `find` 去重和 `make` 阶段的 `weak_ptr` 设备复用。
- 多设备场景用 `separate_device_addr` / `combine_device_addrs` 在「带数字后缀的单地址」与「地址 vector」之间转换，正则 `(\D+\d*\D+)(\d*)` 兼容键名中间含数字的情况。

---

## 7. 下一步学习建议

本讲把 `device_addr_t` / `device_filter_t` / 设备哈希这套「地址与发现」的底层数据讲透了，但用户日常几乎不直接调 `device::find` / `device::make`，而是用更友好的 **`multi_usrp`** 高层 API。下一讲 **u2-l3 multi_usrp 高层 API** 将讲解：

- `multi_usrp::make` 如何在内部调用本讲的 `device::make`，并把得到的 `device` 包成易用对象。
- 子设备规格 `subdev_spec` 与多通道映射规则。
- `multi_usrp` 如何把方法调用翻译成对 `property_tree`（u2-l4）的读写。

建议在本讲掌握后，继续阅读：

- [host/include/uhd/usrp/multi_usrp.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp) 的文档注释，重点看 `make` 与 `subdev_spec` 相关说明。
- [host/lib/usrp/multi_usrp.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp) 中 `multi_usrp::make` 如何用本讲的 `device_addr_t` 调 `device::make`，从而把本讲的底层机制串到用户 API。
