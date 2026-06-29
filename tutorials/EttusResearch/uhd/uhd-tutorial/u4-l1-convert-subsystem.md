# 样本格式转换 convert 子系统

## 1. 本讲目标

本讲是「底层机制深入」单元的第一讲。在 u2-l5 中我们已经建立了流式 API 的接口契约：你给 UHD 一个 `cpu_format`（主机内存里的样本类型，如 `fc32`）和一个 `otw_format`（线缆上的样本类型，如 `sc16`），UHD 就能在两者之间自动转换。但当时我们刻意回避了一个问题——**这层转换是谁做的、怎么做的、为什么很快？**

本讲就钻进这层转换的内部，读完应该掌握：

1. `convert` 子系统的整体架构：转换器（converter）是什么对象、用什么数据结构登记、被谁调用。
2. `converter::id_type` 这个四元组如何唯一标识「一种转换」，以及优先级（priority）如何让多种实现并存且自动择优。
3. 注册表（registry）与工厂函数 `get_converter` 的查找与择优逻辑。
4. SSE2 / AVX2 / NEON 向量化优化在源码与构建系统中的组织方式，以及它们为何能用「同一段转换、多份实现、编译期择一」的方式平滑替换通用实现。

本讲全部基于真实源码，引用的行号对应当前 HEAD `2af4ddb96`。

## 2. 前置知识

阅读本讲前，你需要先建立以下概念（来自前序讲义）：

- **cpu_format 与 otw_format（u2-l5）**：`cpu_format` 描述主机缓冲区里的样本类型，`otw_format` 描述设备线缆上的样本类型。命名约定是「实数/复数 + 浮点/整数 + bit 数」：`fc32` = `complex<float>`、`sc16` = `complex<int16_t>`、`f32` = `float`、`s8` = `int8_t`。
- **rx_streamer / tx_streamer（u2-l5）**：接收与发送流器是抽象接口，真正的 `recv`/`send` 由各设备驱动实现；转换发生在样本进出主机的边界上。
- **CHDR（u1-l2、u3-l1）**：现代 RFNoC 设备（X 系列、N 系列等）线缆上用的是 CHDR 包，其样本载荷的格式后缀是 `_chdr`（如 `sc16_chdr`）；老设备用 VITA 的 `item32` 封装，后缀是 `_item32_le` / `_item32_be`（小端/大端）。
- **类型擦除（u2-l5）**：流式 API 用字符串描述格式，运行期才确定真实类型，因此转换层也必须是「运行期按字符串查表分派」的，而不是编译期模板。

如果你对 SIMD（单指令多数据）这个词陌生：它是 CPU 的一条指令同时处理多个数据的指令集扩展。x86 上叫 SSE2（128 位，一次 4 个 32 位 float）/ AVX2（256 位，一次 8 个 float），ARM 上叫 NEON。把「一个样本乘以缩放系数再截断成 int16」这种循环用 SIMD 重写，可以让吞吐量成倍提升——这正是 convert 子系统优化的核心。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `host/include/uhd/convert.hpp` | 公共头文件。定义转换器抽象基类 `converter`、转换标识 `id_type`，以及注册/查找/字节表三组公共 API。 |
| `host/lib/convert/convert_impl.cpp` | 注册表的实现：`register_converter` / `get_converter` 的择优逻辑、`id_type` 的字符串化与相等比较、`get_bytes_per_item` 的回退查找。 |
| `host/lib/convert/convert_common.hpp` | 内部头文件。定义 `DECLARE_CONVERTER` 宏（一个转换器的「声明 + 自注册」一体化模板）、优先级常量、类型别名（`fc32_t` 等）和可复用的标量转换内联函数。 |
| `host/lib/convert/convert_item32.cpp` | 用宏批量生成「通用（`PRIORITY_GENERAL`）」的 `fc32/sc16/sc64/fc64 ↔ sc16_item32_le/be` 转换器。 |
| `host/lib/convert/sse2_fc32_to_sc16.cpp` 等 `sse2_*` / `avx2_*` 文件 | 向量化优化实现，注册为 `PRIORITY_SIMD`，与通用实现同 id、不同优先级。 |
| `host/lib/convert/CMakeLists.txt` | 构建脚本。按编译器是否支持 AVX2 / SSE2 / NEON，**互斥地**把对应的向量化源文件编译进 libuhd。 |
| `host/lib/include/uhdlib/transport/rx_streamer_impl.hpp`、`tx_streamer_impl.hpp`、`host/lib/transport/super_recv_packet_handler.hpp` | 转换器的**消费者**：流器在初始化时拼出 `id_type`，调 `get_converter` 取回转换器并设置缩放系数。 |

> 注意：`convert_general.cpp`（由 `gen_convert_general.py` 在构建期生成）负责 `_chdr` 等格式的通用实现，源码里看不到 `.cpp`，但能在 `.py` 里读到模板。这点会在 4.4 节用到。

## 4. 核心概念与源码讲解

### 4.1 converter 抽象与公共接口（convert.hpp）

#### 4.1.1 概念说明

「转换」本质上是一个函数：吃进一组输入缓冲、吐出一组输出缓冲、处理 `num` 个样本。但 UHD 不把它写成一个普通函数，而是抽象成一个**多态对象** `converter`。原因有二：

1. **需要携带状态**。浮点 ↔ 定点转换有一个「缩放系数」（scale factor），接收侧用 `1/32767.0`、发送侧用 `32767.0`。这个系数要在创建时设置一次、之后每次转换复用，用对象比用全局变量干净。
2. **需要运行期多态**。同一种转换（如 `fc32 → sc16`）有通用、SSE2、AVX2 多份实现，注册表里存的是它们的工厂函数，运行时按优先级挑一个 `new` 出来，返回基类指针，调用方完全不必关心是哪一份实现。

所以 `converter` 是一个纯虚抽象基类，调用方只看到 `conv(in, out, num)` 这个统一入口。

#### 4.1.2 核心流程

一个 converter 对象的生命周期：

```text
注册期(main 之前)：实现文件用宏把 (id, 工厂函数, 优先级) 登记进全局表
        │
查询期(开流时)：流器拼出 id_type → get_converter(id) 返回工厂函数
        │
实例化：流器调工厂函数 () → new 出具体 converter 子类，返回 converter::sptr
        │
配置：流器调 set_scalar(scale_factor) 设置缩放系数
        │
运行(每批样本)：流器反复调 conv(in, out, num) 做转换
```

注意 `conv` 只是一个薄薄的公开展示层（public façade），真正的计算藏在私有的 `operator()` 里。

#### 4.1.3 源码精读

`converter` 抽象基类定义在公共头文件里：

[host/include/uhd/convert.hpp:21-51](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/convert.hpp#L21-L51) — 定义 `converter` 抽象基类：`set_scalar` 设置缩放系数（纯虚），公开的 `conv` 在 `num != 0` 时转发给私有的 `operator()`，后者才是各子类要实现的「真正干活」的方法。

```cpp
typedef std::shared_ptr<converter> sptr;          // 用 shared_ptr 管理生命周期
typedef uhd::ref_vector<void*> output_type;       // 输出缓冲（可写）
typedef uhd::ref_vector<const void*> input_type;  // 输入缓冲（只读）

virtual void set_scalar(const double) = 0;        // 设置缩放系数

UHD_INLINE void conv(const input_type& in, const output_type& out, const size_t num) {
    if (num != 0)              // num==0 直接跳过，避免无意义调用
        (*this)(in, out, num); // 转发给私有的 operator()
}
```

这里有两个关键设计点：

- **输入用 `ref_vector<const void*>`，输出用 `ref_vector<void*>`**。这与 u2-l5 讲过的「发送缓冲只读、接收缓冲可写」完全对应。`ref_vector` 是 UHD 自家的「指针引用数组」，它不拥有内存，只是把若干裸指针打包成可下标访问的容器——因为多通道时输入/输出是多个缓冲。
- **`operator()` 被设为 `private` 且是纯虚**。外部只能通过 `conv()` 进入，子类必须实现 `operator()`。这是一种「非虚接口模式」（NVI）：公开方法非虚、做前置检查（这里的 `num != 0`），真正逻辑走私有虚函数，便于将来在 `conv` 里统一加日志或校验。

工厂函数与优先级的类型别名紧随其后：

[host/include/uhd/convert.hpp:53-57](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/convert.hpp#L53-L57) — `function_type` 是「无参、返回 `converter::sptr` 的工厂函数」；`priority_type` 就是普通的 `int`。注册表里每个 id 下挂着「优先级 → 工厂函数」的映射。

```cpp
typedef std::function<converter::sptr(void)> function_type; // 工厂函数类型
typedef int priority_type;                                  // 优先级，越大越优先
```

公共 API 三件套（注册 / 查找 / 字节表）的声明：

[host/include/uhd/convert.hpp:82-101](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/convert.hpp#L82-L101) — 声明 `register_converter`（登记一个工厂）、`get_converter`（按 id 查工厂，默认 `prio = -1` 表示「取最高优先级」）、`register_bytes_per_item` / `get_bytes_per_item`（维护格式字符串与每样本字节数的映射）。

```cpp
UHD_API void register_converter(const id_type& id, const function_type& fcn, const priority_type prio);
UHD_API function_type get_converter(const id_type& id, const priority_type prio = -1);
UHD_API void register_bytes_per_item(const std::string& format, const size_t size);
UHD_API size_t get_bytes_per_item(const std::string& format);
```

> `UHD_API` 宏在 u1-l4 讲过，它依 `UHD_DLL_EXPORTS` 在「导出符号」与「导入符号」之间切换，让这套函数能跨越 `.so` 边界被调用。

#### 4.1.4 代码实践

**实践目标**：确认「`conv` 只是门面，真正逻辑在私有 `operator()`」这一设计。

**操作步骤**（源码阅读型）：

1. 打开 `host/include/uhd/convert.hpp`，定位 `converter` 类。
2. 找到 `conv`（公开、非虚）与 `operator()`（私有、纯虚）。
3. 在 `host/lib/convert/` 下任选一个实现（如 `sse2_fc32_to_sc16.cpp`），用编辑器搜索 `operator()`，确认子类实现的是这个私有虚函数。

**需要观察的现象**：实现文件里**不会**重写 `conv`，只重写 `operator()`；调用方（流器）只调 `conv`，从不直接调 `operator()`。

**预期结果**：你会看到 `DECLARE_CONVERTER` 宏展开后生成的就是 `void operator()(...)` 的函数体（见 4.3.3），而 `conv` 在整个 `host/lib/convert/` 目录里没有任何重写——这印证了 NVI 模式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `conv` 要在 `num != 0` 时才调用 `operator()`，而不是直接调用？

**参考答案**：两个原因。一是省掉一次无意义的虚函数调用开销（流式收发时 `recv` 可能返回 0 个样本）；二是部分向量化实现的循环前提是 `i + 3 < nsamps`，`nsamps == 0` 时虽然能正确跳过，但加一层判断可以避免进入函数、保护寄存器等无谓开销。这是一个低开销但高频路径上的常见微优化。

**练习 2**：`input_type` 用 `const void*`、`output_type` 用 `void*`，分别对应收发的哪一侧？

**参考答案**：不对应「收 vs 发」，而是对应「只读 vs 可写」。无论收发，转换器的**输入**始终只读（`const void*`），**输出**始终可写（`void*`）。例如接收侧 `sc16 → fc32`：线缆来的 sc16 是输入（只读），写进主机缓冲的 fc32 是输出（可写）；发送侧 `fc32 → sc16`：主机缓冲的 fc32 是输入（只读），准备上网的 sc16 是输出（可写）。

---

### 4.2 转换标识 id_type 与字节表

#### 4.2.1 概念说明

注册表是一张「key → value」的大字典。value 是「优先级 → 工厂函数」的子字典，那 key 是什么？就是 `id_type`——一个**四元组**，完整描述「这是一种什么转换」：

```text
id_type = ( input_format, num_inputs, output_format, num_outputs )
```

- `input_format` / `output_format`：就是 `cpu_format` 和 `otw_format` 那种字符串，如 `fc32`、`sc16_item32_le`、`sc16_chdr`。
- `num_inputs` / `num_outputs`：输入/输出缓冲的个数（绝大多数转换是 1 进 1 出，即 `num_inputs = num_outputs = 1`）。

**为什么要把 `num_inputs`/`num_outputs` 也放进 key？** 因为理论上可以有一个转换器「把 2 路输入交错合并成 1 路输出」（如两路 sc8 打包进一个 item32）。把它们纳入 key，就让注册表能区分「1 进 1 出的 fc32→sc16」和「2 进 1 出的版本」。

另外，`id_type` 必须支持相等比较（要做字典 key）和可读的字符串化（出错时报错用），所以它继承了 `boost::equality_comparable` 并提供了 `to_pp_string` / `to_string`。

#### 4.2.2 核心流程

`id_type` 的构造不在头文件里写死，而是由**消费者（流器）在开流时动态拼出**：

```text
开流时：
  流器拿到 stream_args.cpu_format（如 "fc32"）和 stream_args.otw_format（如 "sc16"）
  ↓
  拼出 id_type：
    id.input_format  = cpu_format            // "fc32"
    id.num_inputs    = 1
    id.output_format = otw_format + "_chdr"  // RFNoC 设备拼 "sc16_chdr"
    id.num_outputs   = 1
  ↓
  调 get_converter(id) 在注册表里查这个四元组
```

注意 `output_format` 是「运行期字符串拼接」出来的：现代 RFNoC 设备会把 `otw_format` 加上 `_chdr` 后缀（CHDR 载荷格式），老设备则用 `_item32_le`/`_item32_be` 后缀（VITA 封装 + 字节序）。这就解释了为什么实现文件里你会看到 `sc16_chdr`、`sc16_item32_le`、`sc16_item32_be` 三种「同一个 sc16」——它们对应不同的线缆封装，是不同的 id。

#### 4.2.3 源码精读

`id_type` 的定义：

[host/include/uhd/convert.hpp:59-71](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/convert.hpp#L59-L71) — `id_type` 是四个字段的结构体（输入格式、输入数、输出格式、输出数），继承 `boost::equality_comparable` 以自动获得 `!=`，并声明 `operator==` 在 convert_impl.cpp 实现。

```cpp
struct UHD_API id_type : boost::equality_comparable<id_type> {
    std::string input_format;
    size_t num_inputs;
    std::string output_format;
    size_t num_outputs;
    std::string to_pp_string(void) const;  // 多行可读形式，用于报错
    std::string to_string(void) const;     // 单行紧凑形式
};
UHD_API bool operator==(const id_type&, const id_type&);
```

相等比较的实现——逐字段比较四个成员：

[host/lib/convert/convert_impl.cpp:24-30](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_impl.cpp#L24-L30) — `operator==` 对四个字段逐一比较（开头的 `true and ...` 是 UHD 代码风格，便于注释掉某一行来临时放宽匹配）。

```cpp
bool convert::operator==(const convert::id_type& lhs, const convert::id_type& rhs) {
    return true and (lhs.input_format == rhs.input_format)
           and (lhs.num_inputs == rhs.num_inputs)
           and (lhs.output_format == rhs.output_format)
           and (lhs.num_outputs == rhs.num_outputs);
}
```

报错用的字符串化（当 `get_converter` 找不到时，会把这段打印出来，告诉你它在找什么转换）：

[host/lib/convert/convert_impl.cpp:32-47](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_impl.cpp#L32-L47) — `to_pp_string` 输出多行的「conversion ID」报告；`to_string` 输出 `fc32 (1) -> sc16_item32_le (1)` 这样的紧凑串。

字节表是另一套独立的注册表，和转换器注册表平行。它的作用是回答「某个格式字符串，一个样本占几个字节」——流器需要它来计算缓冲区大小、每包样本数等。它在 `main` 之前用一个静态块集中登记所有标准类型：

[host/lib/convert/convert_impl.cpp:137-158](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_impl.cpp#L137-L158) — `convert_register_item_sizes` 静态块把 `fc64/fc32/sc64/sc32/sc16/sc8` 等复数类型、`f64/f32/s32/...` 等实数类型，以及 VITA 的 `item32`，逐一登记到字节表。

```cpp
convert::register_bytes_per_item("fc32", sizeof(std::complex<float>));   // 8 字节
convert::register_bytes_per_item("sc16", sizeof(std::complex<int16_t>)); // 4 字节
convert::register_bytes_per_item("item32", sizeof(int32_t));             // 4 字节
```

`get_bytes_per_item` 有个巧妙的回退：如果精确字符串查不到，就按 `_` 截断前缀再查一次：

[host/lib/convert/convert_impl.cpp:120-135](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_impl.cpp#L120-L135) — 先精确查 `format`；查不到则找第一个 `_`，用前缀递归再查。这样 `sc16_item32_le` 这种「带后缀」的格式也能命中 `sc16` 的字节大小。

```cpp
const size_t pos = format.find("_");
if (pos != std::string::npos) {
    return get_bytes_per_item(format.substr(0, pos)); // "sc16_item32_le" → "sc16"
}
throw uhd::key_error("[convert] Cannot find an item size for: `" + format + "'");
```

#### 4.2.4 代码实践

**实践目标**：亲手验证「`sc16_chdr` 与 `sc16_item32_le` 字节数相同」。

**操作步骤**：

1. 阅读 `convert_impl.cpp:137-158`，确认 `sc16` 登记为 `sizeof(complex<int16_t>)`。
2. 想象运行期调用 `get_bytes_per_item("sc16_chdr")`：精确查 `sc16_chdr` 失败 → 按 `_` 截断得 `sc16` → 递归查 `sc16` 成功，返回 4。
3. 同理推演 `get_bytes_per_item("sc16_item32_le")`：截断得 `sc16` → 返回 4。

**需要观察的现象**：虽然线缆封装不同（`_chdr` vs `_item32_le`），但它们承载的都是 `sc16` 样本，每样本 4 字节。

**预期结果**：两种格式都返回 4。这正是回退逻辑的意义——`num_inputs/num_outputs` 维度的字节数只取决于「基类型」，与封装后缀无关。

#### 4.2.5 小练习与答案

**练习 1**：若有人调用 `get_bytes_per_item("totally_unknown")`，会发生什么？

**参考答案**：精确查失败，按 `_` 截断：`"totally_unknown"` 含 `_`，前缀 `"totally"` 再递归查；`"totally"` 不含 `_` 且不在表里，于是抛出 `uhd::key_error("[convert] Cannot find an item size for: 'totally'")`（注意报错的是截断后的 `totally`，不是原名——这是阅读源码才能预知的细节）。

**练习 2**：为什么 `id_type` 的相等比较不包含优先级？

**参考答案**：因为优先级不是「这个 id 是什么」的属性，而是「这个 id 有哪些实现、谁更好」的属性。注册表的结构是 `dict<id_type, dict<priority, function>>`：外层用 id 做 key，内层用优先级做 key。两个 id 相等当且仅当四元组相同；至于它下面挂了几个优先级的实现，是另一回事。把优先级排除在相等比较之外，才能让「同 id 多实现」在表里自然并存。

---

### 4.3 注册表、工厂与 DECLARE_CONVERTER 宏（convert_impl + convert_common）

#### 4.3.1 概念说明

现在 key（`id_type`）有了，value 是什么？是「优先级 → 工厂函数」的内层字典。整个注册表是一张**两级字典**：

```text
注册表 = {
    id_type( fc32,1, sc16_item32_le,1 ) : {
        0  : <通用工厂>,     # PRIORITY_GENERAL
        3  : <AVX2 工厂>,    # PRIORITY_SIMD（或 SSE2 工厂，二选一）
    },
    id_type( sc16_item32_le,1, fc32,1 ) : { 0: <通用>, 3: <SIMD> },
    ...
}
```

这套机制有三个精妙之处：

1. **自注册（self-registration）**。每个实现文件用 `UHD_STATIC_BLOCK` 宏定义一个「在 `main` 之前执行的静态块」，自己把自己登记进表里。没有中央清单，加一个新转换只要新写一个文件、链进来即可。这和 u2-l1 讲的设备自注册是同一套思想。
2. **同 id 多实现并存**。通用实现和 SIMD 实现注册到**同一个 id** 下，只是优先级不同。运行期 `get_converter` 默认取最高优先级，于是 SIMD 实现透明地「盖过」通用实现。
3. **DECLARE_CONVERTER 宏把「声明一个子类 + 自注册 + 定义 operator()」三件事打包成一行**，让写一个转换器像写一个函数一样简单。

#### 4.3.2 核心流程

注册表的查找与择优逻辑（`get_converter`）：

```text
get_converter(id, prio = -1):
  若表里没有 id            → 抛 key_error（附 id.to_pp_string()）
  遍历该 id 下所有已登记的优先级 prio_i：
    若 prio_i == 用户指定 prio → 立即返回该工厂（精确匹配）
    否则记录 best_prio = max(best_prio, prio_i)
  若用户指定了 prio(≠-1) 但没找到 → 抛 key_error
  否则返回 best_prio 对应的工厂（自动择优）
```

一句话：**`prio = -1`（默认）就是「给我这个 id 下最好的实现」**。这就是为什么流器初始化时只写 `get_converter(id)`、不指定优先级，却能自动拿到 SIMD 实现。

优先级的取值是约定俗成的整数常量，定义在内部头 `convert_common.hpp`：

```text
PRIORITY_EMPTY  = -1   # 空实现（占位）
PRIORITY_GENERAL = 0   # 通用标量实现（一定能跑）
PRIORITY_TABLE   = 1   # 查表实现（sc16→sc8 等）
PRIORITY_SIMD    = 3   # x86 的 SSE2/AVX2（ARM 上为 2，因查表在 ARM 上反而慢）
```

#### 4.3.3 源码精读

注册表的数据结构——一个单例的两级字典：

[host/lib/convert/convert_impl.cpp:49-55](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_impl.cpp#L49-L55) — `fcn_table_type` 是 `dict<id_type, dict<priority_type, function_type>>`，用 `UHD_SINGLETON_FCN` 做成单例 `get_table()`。外层按 id 索引，内层按优先级索引。

```cpp
typedef uhd::dict<convert::id_type,
    uhd::dict<convert::priority_type, convert::function_type>> fcn_table_type;
UHD_SINGLETON_FCN(fcn_table_type, get_table);
```

登记一个转换器，就是往内层字典塞一条：

[host/lib/convert/convert_impl.cpp:60-69](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_impl.cpp#L60-L69) — `register_converter(id, fcn, prio)` 执行 `get_table()[id][prio] = fcn`。若同一 `(id, prio)` 登记两次，后者覆盖前者。

工厂查找与择优——本子系统最核心的一段逻辑：

[host/lib/convert/convert_impl.cpp:74-107](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_impl.cpp#L74-L107) — `get_converter`：先确认 id 存在；再遍历该 id 下所有优先级，若遇到与 `prio` 精确相等者立即返回；否则在遍历中累计 `best_prio`；最后若 `prio == -1` 则返回 `best_prio` 对应工厂，若指定了 prio 却没找到则抛错。

```cpp
convert::function_type convert::get_converter(const id_type& id, const priority_type prio) {
    if (not get_table().has_key(id))
        throw uhd::key_error("Cannot find a conversion routine for " + id.to_pp_string());

    priority_type best_prio = -1;
    for (priority_type prio_i : get_table()[id].keys()) {
        if (prio_i == prio) return get_table()[id][prio];   // 精确匹配
        best_prio = std::max(best_prio, prio_i);            // 记录最高
    }
    if (prio != -1)
        throw uhd::key_error("Cannot find a conversion routine [with prio] for " + id.to_pp_string());
    return get_table()[id][best_prio];                      // 自动取最高优先级
}
```

`DECLARE_CONVERTER` 宏——写转换器的「语法糖」。它把一个看起来像函数定义的语法块，展开成「一个子类 + 一个静态注册块 + operator() 函数体」：

[host/lib/convert/convert_common.hpp:17-41](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_common.hpp#L17-L41) — `_DECLARE_CONVERTER` 宏：定义一个继承 `converter` 的子类 `name`，内含静态工厂 `make()`、`scale_factor` 成员、`set_scalar`、以及 `operator()` 的声明；紧接着用 `UHD_STATIC_BLOCK` 在 `main` 前把 `(id, &name::make, prio)` 登记进注册表；最后留下 `operator()` 的函数头等你写函数体。

```cpp
#define _DECLARE_CONVERTER(name, in_form, num_in, out_form, num_out, prio)       \
    struct name : public uhd::convert::converter {                               \
        static sptr make(void) { return sptr(new name()); }                      \
        double scale_factor;                                                     \
        void set_scalar(const double s) { scale_factor = s; }                    \
        void operator()(const input_type&, const output_type&, const size_t);    \
    };                                                                           \
    UHD_STATIC_BLOCK(__register_##name##_##prio) {                               \
        uhd::convert::id_type id;                                                \
        id.input_format  = #in_form;                                             \
        id.num_inputs    = num_in;                                               \
        id.output_format = #out_form;                                            \
        id.num_outputs   = num_out;                                              \
        uhd::convert::register_converter(id, &name::make, prio);                 \
    }                                                                            \
    void name::operator()(const input_type& inputs, const output_type& outputs, const size_t nsamps)
```

[host/lib/convert/convert_common.hpp:57-63](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_common.hpp#L57-L63) — `DECLARE_CONVERTER` 是上层便捷宏：用各参数拼出唯一的子类名 `__convert_<in>_<nin>_<out>_<nout>_<prio>`，再转发给 `_DECLARE_CONVERTER`。于是写一个转换器只需 `DECLARE_CONVERTER(fc32, 1, sc16_item32_le, 1, PRIORITY_SIMD) { ...函数体... }`。

优先级常量与一个值得注意的平台分支：

[host/lib/convert/convert_common.hpp:65-79](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_common.hpp#L65-L79) — 定义 `PRIORITY_GENERAL=0`、`PRIORITY_EMPTY=-1`；在 ARM（`__ARM_NEON__`）下 `PRIORITY_SIMD=2` 且 `PRIORITY_TABLE=1`（查表因缓存压力在 ARM 上更慢，故让 SIMD 仍高于表但低于 x86 的设定）；x86 下 `PRIORITY_SIMD=3`（注释提到「曾经还有 ORC，故 SIMD 取 3」）。

通用实现如何用宏批量生成。`convert_item32.cpp` 用一层宏把「每个 cpu 类型 × sc16/sc8 × le/be」组合一次性铺开：

[host/lib/convert/convert_item32.cpp:11-39](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_item32.cpp#L11-L39) — `__DECLARE_ITEM32_CONVERTER` 用 `DECLARE_CONVERTER(..., PRIORITY_GENERAL)` 生成「cpu→wire」和「wire→cpu」一对通用转换器（`be` 用 `htonx/ntohx` 大端字节序，`le` 用 `htowx/wtohx` 小端）；外层 `DECLARE_ITEM32_CONVERTER(cpu)` 再把 sc8、sc16 两种 wire 类型展开；最后三行实例化 `sc16/fc32/fc64` 三个 cpu 类型，一次性登记了它们与 `sc16_item32_le/be`、`sc8_item32_le/be` 之间的全部通用转换。

```cpp
DECLARE_ITEM32_CONVERTER(sc16)   /* sc16<->sc16,sc8(otw) */
DECLARE_ITEM32_CONVERTER(fc32)   /* fc32<->sc16,sc8(otw) */
DECLARE_ITEM32_CONVERTER(fc64)   /* fc64<->sc16,sc8(otw) */
_DECLARE_ITEM32_CONVERTER(sc8, sc8)
```

最后看消费者如何使用。RFNoC 发送流器在初始化时拼 id、取工厂、设置缩放系数：

[host/lib/include/uhdlib/transport/tx_streamer_impl.hpp:458-486](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/transport/tx_streamer_impl.hpp#L458-L486) — 发送侧：`id.input_format = cpu_format`（如 `fc32`）、`id.output_format = otw_format + "_chdr"`（如 `sc16_chdr`），然后 `get_converter(id)()` 实例化、`set_scalar(32767.0)` 设置发送侧缩放系数（把 `[-1,1]` 的浮点放大到 `[-32767,32767]` 的 int16）。注释说明现在不必再指定字节序，因为可以让线缆字节序匹配主机字节序。

```cpp
convert::id_type id;
id.input_format  = stream_args.cpu_format;          // "fc32"
id.num_inputs    = 1;
id.output_format = stream_args.otw_format + "_chdr";// "sc16_chdr"
id.num_outputs   = 1;
...
_converters.push_back(convert::get_converter(id)()); // 工厂实例化
_converters.back()->set_scalar(32767.0);             // 发送侧放大系数
```

接收侧对称地用 `1/32767.0`：

[host/lib/transport/super_recv_packet_handler.hpp:174-182](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L174-L182) — 接收侧 `set_converter`：`get_converter(id)()` 实例化后，立即 `set_scale_factor(1/32767.)`（接收侧把 int16 缩回 `[-1,1]` 浮点），并用 `get_bytes_per_item` 记录 otw/cpu 两端的每样本字节数。

```cpp
void set_converter(const uhd::convert::id_type& id) {
    _num_outputs = id.num_outputs;
    _converter   = uhd::convert::get_converter(id)();
    this->set_scale_factor(1 / 32767.);                       // 接收侧缩小系数
    _bytes_per_otw_item = uhd::convert::get_bytes_per_item(id.input_format);
    _bytes_per_cpu_item = uhd::convert::get_bytes_per_item(id.output_format);
}
```

#### 4.3.4 代码实践

**实践目标**：跟踪一次「`fc32` 收 → 注册表择优 → 命中 SIMD 实现」的完整链路。

**操作步骤**（源码阅读型）：

1. 假设接收流：`cpu_format = "fc32"`、`otw_format = "sc16"`、RFNoC 设备。
2. 按 `super_recv_packet_handler.hpp:174` 推出 id：`(sc16_chdr, 1, fc32, 1)`（注意接收侧 input 是线缆、output 是主机）。
3. 查注册表：该 id 下挂着哪些优先级？
   - `PRIORITY_GENERAL (0)`：由 `gen_convert_general.py` 的 `TMPL_CONV_CHDR_SC16_TO_FP` 模板生成（`sc16_chdr → fc32`）。
   - `PRIORITY_SIMD (3)`：在 x86 AVX2 构建里**没有** `sc16_chdr→fc32` 的 SIMD 版（AVX2/SSE2 只为 `sc16_item32_le/be→fc32` 提供 SIMD，见 4.4）；故该 id 只有 GENERAL。
4. 对照 `get_converter` 逻辑：`prio=-1` → `best_prio=0` → 返回通用实现。

**需要观察的现象**：不同 wire 封装（`_chdr` vs `_item32_le`）的 SIMD 覆盖范围不同。`sc16_chdr→fc32`（现代 RFNoC 接收）目前只有通用实现，而老式的 `sc16_item32_le→fc32` 才有 SIMD 加速。

**预期结果**：能说出「RFNoC 设备收 fc32 时，转换走的是 `gen_convert_general.py` 生成的通用循环，而非 SSE2/AVX2」——这是阅读源码后才能得到的、反直觉但真实的结论。

> 说明：上面是「待本地验证」的推断——是否真的没有 `sc16_chdr` 的 SIMD 反向实现，建议你用下一条命令在本地 grep 确认（见综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：如果同一 `(id, prio)` 被两个文件各登记一次，会发生什么？

**参考答案**：`register_converter` 执行的是 `get_table()[id][prio] = fcn`，是赋值而非追加，所以**后登记者覆盖先登记者**。由于静态块的执行顺序在翻译单元之间不确定，重复登记同一 `(id, prio)` 是危险的——这正是为什么不同实现必须用**不同优先级**来区分（如 GENERAL 与 SIMD），而不是都用同一个优先级。

**练习 2**：`get_converter(id, prio)` 中，为什么先做「精确匹配 prio_i == prio」的提前返回，再退而求「best_prio」？

**参考答案**：支持「强制指定优先级」的用法。默认 `prio=-1` 表示「我要最好的」，走 best_prio 分支；但测试或调优时可能想强制跑通用实现（`prio=0`）来对比性能或排查 SIMD 实现的 bug，这时传具体 prio 就能精确命中。提前返回避免在精确命中时还做无谓的遍历。

---

### 4.4 向量优化实现：SSE2 / AVX2 / NEON

#### 4.4.1 概念说明

通用实现（`PRIORITY_GENERAL`）是一条朴素的标量 `for` 循环：逐样本读 float、乘缩放系数、截断成 int16、写回。它一定正确、一定可移植，但慢——一个样本要做一次乘法、一次截断、一次内存读写，CPU 的向量单元闲置。

向量化优化把这条循环用 SIMD 指令重写：一条指令同时处理 4 个（SSE2，128 位）或 8 个（AVX2，256 位）样本。同一个转换 id，通用实现注册为 `PRIORITY_GENERAL=0`，SIMD 实现注册为 `PRIORITY_SIMD=3`，运行期 `get_converter` 自动挑出 SIMD 那份——对调用方完全透明。

这套设计的关键约束是：**SIMD 指令集是平台相关的**。AVX2 指令在不支持 AVX2 的 CPU 上会触发非法指令异常，所以不能把 AVX2 代码无条件编译进 libuhd。UHD 的解法是「编译期互斥」：构建系统（CMake）检测本机 CPU 支持哪种指令集，**只编译对应的那一组源文件**。SSE2 与 AVX2 互斥（CMake 用 `elseif`），ARM 上则编译 NEON 版本。

#### 4.4.2 核心流程

向量化版本的通用骨架（以 `fc32 → sc16_item32_le` 为例）：

```text
1. 把缩放系数 broadcast 成一个向量常量（如 __m128 scalar = 4 个相同的 float）
2. 主循环：每次处理 4 个(SSE2) 或 8 个(AVX2) 样本
     a. load：把连续 4/8 个 float 装进向量寄存器
     b. mul：整向量乘以 scalar
     c. cvt：浮点 → int32（_mm_cvtps_epi32）
     d. pack：int32 → int16（_mm_packs_epi32，自带饱和截断）
     e. 字节序/通道顺序整理：shuffle 或 byte-swap
     f. store：写回输出缓冲
3. 尾部：剩下不足 4/8 个的样本，调用通用标量函数 xx_to_item32_sc16 收尾
4. 对齐分发：根据输入指针的 16 字节对齐情况，选对齐 load(_mm_load) 或非对齐 load(_mm_loadu)
```

第 3、4 步是 SIMD 代码的两个「工程难点」：样本数不一定是 4/8 的整数倍（要有尾部处理），输入指针不一定对齐（要对齐分发）。`convert_common.hpp` 提供了可复用的标量收尾函数，避免每个 SIMD 文件重复写。

#### 4.4.3 源码精读

先看 SSE2 实现（一次 4 样本，含对齐分发与尾部收尾）：

[host/lib/convert/sse2_fc32_to_sc16.cpp:15-68](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/sse2_fc32_to_sc16.cpp#L15-L68) — `fc32 → sc16_item32_le`（`PRIORITY_SIMD`）：把缩放系数广播成 `__m128`，主循环每次 `_mm_load` 4 个 float、`_mm_mul_ps` 乘系数、`_mm_cvtps_epi32` 转 int32、`_mm_packs_epi32` 饱和压成 int16、再 `_mm_shufflelo/hi_epi16` 调整 I/Q 通道顺序后写回。`switch (size_t(input) & 0xf)` 按输入指针的 16 字节对齐情况分发到对齐 load(`_`) 或非对齐 load(`u_`)；末尾用通用函数 `xx_to_item32_sc16<uhd::htowx>(...)` 处理不足 4 个的尾巴。

```cpp
const __m128 scalar = _mm_set_ps1(float(scale_factor));
for (; i + 3 < nsamps; i += 4) {                 // 每次 4 个样本
    __m128 tmplo = _mm_load_ps(...input + i + 0);
    __m128 tmphi = _mm_load_ps(...input + i + 2);
    __m128i tmpilo = _mm_cvtps_epi32(_mm_mul_ps(tmplo, scalar)); // 乘 + 转 int32
    __m128i tmpihi = _mm_cvtps_epi32(_mm_mul_ps(tmphi, scalar));
    __m128i tmpi = _mm_packs_epi32(tmpilo, tmpihi);              // 饱和压成 int16
    tmpi = _mm_shufflelo_epi16(tmpi, _MM_SHUFFLE(2,3,0,1));      // I/Q 顺序整理
    _mm_storeu_si128(reinterpret_cast<__m128i*>(output + i), tmpi);
}
```

注意大端版本用 `_mm_or_si128(_mm_srli_epi16(...,8), _mm_slli_epi16(...,8))` 做 16 位字的字节交换，而小端版本只需 shuffle（见同文件 `sc16_item32_be` 版本，第 70-122 行）。

AVX2 版本把「每次 4 样本」升级为「每次 8 样本」（256 位寄存器）：

[host/lib/convert/avx2_fc32_to_sc16.cpp:13-49](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/avx2_fc32_to_sc16.cpp#L13-L49) — AVX2 版 `fc32 → sc16_item32_le`（同为 `PRIORITY_SIMD`）：用 `__m256`（256 位）、`_mm256_loadu_ps`、`_mm256_cvtps_epi32`、`_mm256_packs_epi32`，每次循环 `i += 8`。由于 AVX2 的 `_mm256_packs_epi32` 跨 128 位 lane 的打包顺序不是顺序的，额外用 `_mm256_permute2x128_si256` 重排 lane，再 pack。

```cpp
const __m256 scalar = _mm256_set1_ps(float(scale_factor));
for (; i + 7 < nsamps; i += 8) {                 // 每次 8 个样本
    __m256 tmplo = _mm256_loadu_ps(...input + i + 0);
    __m256 tmphi = _mm256_loadu_ps(...input + i + 4);
    __m256i tmpilo = _mm256_cvtps_epi32(_mm256_mul_ps(tmplo, scalar));
    __m256i tmpihi = _mm256_cvtps_epi32(_mm256_mul_ps(tmphi, scalar));
    __m256i shuffled_lo = _mm256_permute2x128_si256(tmpilo, tmpihi, 0x20); // 重排 lane
    __m256i shuffled_hi = _mm256_permute2x128_si256(tmpilo, tmpihi, 0x31);
    __m256i tmpi = _mm256_packs_epi32(shuffled_lo, shuffled_hi);           // 饱和压 int16
    ...
}
xx_to_item32_sc16<uhd::htowx>(input + i, output + i, nsamps - i, scale_factor); // 尾部收尾
```

> 两个文件都注册为 `PRIORITY_SIMD`、登记到**同一个 id** `(fc32,1,sc16_item32_le,1)`。它俩不会同时进 libuhd——由下面的 CMake 决定。

SIMD 收尾用的标量函数定义在 `convert_common.hpp`，供所有向量化版本复用：

[host/lib/convert/convert_common.hpp:111-138](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/convert_common.hpp#L111-L138) — `xx_to_item32_sc16<to_wire>` 是可复用的标量循环：逐样本调 `xx_to_item32_sc16_x1`（乘系数 + `clamp<int16_t>` 饱和截断 + 打包成 item32），再 `to_wire` 做字节序转换。SIMD 主循环处理不了的那个「尾巴」就交给它。`clamp` 模板（第 100-106 行）做饱和截断，是「float 乘完可能越界」的安全网。

构建系统的「编译期互斥择一」是这套设计的基石：

[host/lib/convert/CMakeLists.txt:43-87](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/CMakeLists.txt#L43-L87) — 若检测到 `immintrin.h`（AVX2 可用），把 `avx2_*.cpp` 一组文件用 `-mavx2` 编译进库；**否则若** `emmintrin.h` 可用（SSE2），改用 `sse2_*.cpp` 一组并用 `-msse2` 编译。`elseif` 保证 AVX2 与 SSE2 **互斥**，二者绝不会同时进库——于是同一个 id 下永远只有一份 `PRIORITY_SIMD` 实现，不会发生「两份优先级 3 互相覆盖」的混乱。

```cmake
if(HAVE_IMMINTRIN_H)
    set(convert_with_avx2_sources avx2_sc16_to_sc16.cpp avx2_fc32_to_sc16.cpp ...)
    set_source_files_properties(... PROPERTIES COMPILE_FLAGS "${IMMINTRIN_FLAGS}") # -mavx2
    LIBUHD_APPEND_SOURCES(${convert_with_avx2_sources})
elseif(HAVE_EMMINTRIN_H)
    set(convert_with_sse2_sources sse2_sc16_to_sc16.cpp sse2_fc32_to_sc16.cpp ...)
    set_source_files_properties(... PROPERTIES COMPILE_FLAGS "${EMMINTRIN_FLAGS}") # -msse2
    LIBUHD_APPEND_SOURCES(${convert_with_sse2_sources})
endif()
```

ARM 上的 NEON 走另一条分支（且只在 32 位 ARM 上启用）：

[host/lib/convert/CMakeLists.txt:104-119](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/convert/CMakeLists.txt#L104-L119) — 当 `NEON_SIMD_ENABLE` 开启、`arm_neon.h` 可用、且 `CMAKE_SIZEOF_VOID_P == 4`（32 位）时，把 `convert_with_neon.cpp` 与一段手写汇编 `convert_neon.S` 编译进库。NEON 实现同样注册为 `PRIORITY_SIMD`（在 ARM 上取值为 2）。

完整的优先级选择关系（结合 4.3.2 的常量）可总结为下表：

| 平台 / 构建选项 | 通用实现 | 向量化实现 | `get_converter(prio=-1)` 取到 |
|----------------|---------|-----------|------------------------------|
| x86 + AVX2 | `PRIORITY_GENERAL=0` | AVX2（`PRIORITY_SIMD=3`） | AVX2（3 > 0） |
| x86 + 仅 SSE2 | `PRIORITY_GENERAL=0` | SSE2（`PRIORITY_SIMD=3`） | SSE2（3 > 0） |
| x86 无 SIMD | `PRIORITY_GENERAL=0` | 无 | 通用（0） |
| ARM 32-bit + NEON | `PRIORITY_GENERAL=0` | NEON（`PRIORITY_SIMD=2`） | NEON（2 > 0） |

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：在 `host/lib/convert/` 下统计「`fc32 ↔ sc16`」到底有多少种优先级实现，并说明选择逻辑。

**操作步骤**：

1. 用 grep 列出所有声明 `fc32 → sc16*` 方向的转换器：

```bash
grep -rn "DECLARE_CONVERTER(fc32" host/lib/convert/
```

2. 再列出反向 `sc16* → fc32`：

```bash
grep -rn "DECLARE_CONVERTER(sc16" host/lib/convert/ | grep fc32
```

3. 对每条命中，记录它的「wire 封装（`sc16_item32_le` / `sc16_item32_be` / `sc16_chdr`）」与「优先级（`PRIORITY_GENERAL` / `PRIORITY_SIMD`）」。
4. 对照 `convert_item32.cpp`（通用 `item32_le/be`）、`gen_convert_general.py`（通用 `chdr`）、`sse2_fc32_to_sc16.cpp` / `avx2_fc32_to_sc16.cpp`（SIMD）、`convert_with_neon.cpp`（NEON），把结果填进下表。

**需要观察的现象**：对于同一个 id（如 `(fc32,1,sc16_item32_le,1)`），注册表里同时存在**通用（0）**与**SIMD（3）**两条；SSE2 与 AVX2 不会同时存在（CMake 互斥）。

**预期结果**（以 `fc32 → sc16_item32_le` 这一具体 id 为例）：

| 来源文件 | 优先级 | 是否进库（取决于构建） |
|---------|--------|----------------------|
| `convert_item32.cpp`（宏生成） | `PRIORITY_GENERAL = 0` | 总是进库 |
| `avx2_fc32_to_sc16.cpp` | `PRIORITY_SIMD = 3` | 仅 AVX2 构建 |
| `sse2_fc32_to_sc16.cpp` | `PRIORITY_SIMD = 3` | 仅 SSE2 构建（与 AVX2 互斥） |
| `convert_with_neon.cpp` | `PRIORITY_SIMD = 2` | 仅 ARM NEON 构建 |

**选择逻辑说明**：在任一具体构建里，该 id 下最多挂两条：通用（0）+ 一条 SIMD（x86 为 3，ARM 为 2）。流器调用 `get_converter(id)`（`prio=-1`）时，按 `convert_impl.cpp:74-107` 的逻辑，`best_prio` 取到 SIMD 那条；只有当构建完全不含 SIMD 时，才退回通用（0）。这就是「同 id 多实现、按优先级择优、对调用方透明」的完整闭环。

**反向 `sc16_chdr → fc32` 的额外发现**：grep 会显示 SSE2/AVX2 文件里**没有** `sc16_chdr` 反向声明（只有 `sc16_item32_le/be`），故现代 RFNoC 设备接收 `fc32` 时，转换走的是 `gen_convert_general.py` 生成的通用标量循环。是否真的如此，请你用上面的 grep 命令在本地确认（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 SSE2 实现里要 `switch (size_t(input) & 0xf)` 做对齐分发，AVX2 实现却直接用 `_mm256_loadu_ps`（非对齐 load）？

**参考答案**：这是一个「年代与权衡」的差异。SSE2 时代，对齐 load（`_mm_load_ps`）比非对齐 load 显著快，所以值得写一段对齐分发：先处理掉未对齐的头部样本把指针「掰」到 16 字节对齐，主体走对齐 load。AVX2 时代，硬件对非对齐访问的惩罚已大幅降低，`_mm256_loadu_ps` 在多数情况下已足够快，所以代码选择直接非对齐 load、省去分发逻辑，换取代码简洁。两种写法都是各自时代的合理工程选择。

**练习 2**：若你在一台不支持 AVX2 的老 x86 CPU 上运行 UHD，`fc32 → sc16_item32_le` 会用哪份实现？为什么不会崩？

**参考答案**：用 SSE2 实现（`PRIORITY_SIMD=3`），退不到通用。不崩的原因在**构建期**而非运行期：CMake 在编译 UHD 的那台机器上检测到没有 `immintrin.h`（AVX2 不可用）、但有 `emmintrin.h`（SSE2 可用），于是只把 `sse2_*.cpp` 编译进库、根本不编译 `avx2_*.cpp`，所以库里不存在任何 AVX2 指令。注意隐含前提：**编译机的指令集应与运行机兼容**，CMakeLists 注释 "Runtime target must support AVX2!" 也警示了这一点。

## 5. 综合实践

把本讲四个模块串起来，完成一次「从开流到转换」的全链路推演与验证。

**任务**：假设你要在一台 x86 AVX2 机器上，用 RFNoC 设备做发送：`cpu_format = "fc32"`、`otw_format = "sc16"`。请完成下面四步。

1. **拼 id**：参照 `tx_streamer_impl.hpp:458-462`，写出运行期拼出的 `id_type` 四元组（注意发送侧 input 是主机、output 是线缆，且 RFNoC 加 `_chdr` 后缀）。
2. **查注册表**：在源码里找出这个 id 下登记的所有实现及其优先级（提示：`fc32 → sc16_chdr` 的通用版来自 `gen_convert_general.py`，SIMD 版来自 `sse2/avx2_fc32_to_sc16.cpp`）。
3. **推择优结果**：按 `get_converter`（`convert_impl.cpp:74-107`）逻辑，判断本次构建会取到哪份实现、优先级是多少。
4. **本地验证**：构建 UHD 后，用下面命令确认你的推断（**待本地验证**）：
   ```bash
   # 看本机构建启用了哪种 SIMD
   uhd_config_info --print-all | grep -i -E "avx|sse|neon"
   ```
   并用 4.4.4 的 grep 命令核对源码侧的覆盖范围，解释「构建报告」与「源码覆盖」是否一致。

**参考推演**：

1. id = `(input_format="fc32", num_inputs=1, output_format="sc16_chdr", num_outputs=1)`。
2. 该 id 下：通用版（`gen_convert_general.py` 的 `TMPL_CONV_CHDR_SC16_TO_FP`，`PRIORITY_GENERAL=0`）+ SIMD 版（`sse2_fc32_to_sc16.cpp` 或 `avx2_fc32_to_sc16.cpp` 的 `DECLARE_CONVERTER(fc32,1,sc16_chdr,1,PRIORITY_SIMD)`）。注意发送方向 `fc32→sc16_chdr` 的 SIMD 版**确实存在**（见 `sse2_fc32_to_sc16.cpp:124` 与 `avx2_fc32_to_sc16.cpp:87`），与接收方向 `sc16_chdr→fc32` 缺 SIMD 的情况不同。
3. AVX2 构建下 `best_prio = 3`，取到 AVX2 的 `fc32→sc16_chdr` 实现，缩放系数 `set_scalar(32767.0)`。
4. 若 `uhd_config_info` 报告启用了 AVX2，则发送转换走 AVX2 8 样本/拍循环；二者一致。

> 这个练习揭示了一个容易忽略的事实：**发送方向与接收方向的 SIMD 覆盖并不对称**——发送 `fc32→sc16_chdr` 有 SIMD，接收 `sc16_chdr→fc32` 在本 HEAD 下没有。这正是「读源码胜过想当然」的典型例子。

## 6. 本讲小结

- `convert` 子系统用「类型擦除 + 运行期查表」实现 cpu_format 与 otw_format 之间的转换：`converter` 是纯虚抽象基类，公开的 `conv` 是 NVI 门面，真正逻辑在私有 `operator()`。
- 转换用四元组 `id_type = (input_format, num_inputs, output_format, num_outputs)` 唯一标识；消费者（流器）在开流时用字符串拼接动态生成 id（RFNoC 加 `_chdr` 后缀、老设备用 `_item32_le/be`）。
- 注册表是两级字典 `dict<id_type, dict<priority, function>>`；`get_converter(id, prio=-1)` 默认返回该 id 下**最高优先级**的工厂，这让「同 id 多实现、自动择优」对调用方完全透明。
- `DECLARE_CONVERTER` 宏把「声明子类 + 静态自注册 + 定义 operator()」三合一，配合 `UHD_STATIC_BLOCK` 实现无需中央清单的自注册；优先级常量 `PRIORITY_GENERAL=0` / `PRIORITY_SIMD=3(x86)或2(ARM)` / `PRIORITY_TABLE=1`。
- 向量化优化（SSE2/AVX2/NEON）与通用实现注册到**同一 id、不同优先级**；CMake 按 CPU 指令集**互斥**地编译对应源文件，保证同 id 下永远只有一份 SIMD 实现；SIMD 主循环处理对齐与批量，尾部样本交给 `convert_common.hpp` 的标量函数收尾。
- 字节表 `get_bytes_per_item` 与转换表平行，用「按 `_` 截断前缀递归」的回退策略，让 `sc16_item32_le`、`sc16_chdr` 这类带后缀的格式都能命中基类型 `sc16` 的字节数。

## 7. 下一步学习建议

本讲讲清了「样本在进出主机时如何被转换」。沿着底层机制单元继续往下：

- **u4-2 传输层**：转换后的样本是如何经 UDP zero-copy / USB / DPDK 在主机与设备之间搬运的？转换器写出的输出缓冲正是传输层要发送的载荷。
- **u4-3 VRT 包协议**：`sc16_item32_le/be` 的 `item32` 封装来自 VITA/VRT 包格式；那里会讲 VRT 包头如何解析、`super_recv_packet_handler` 如何批量收包并调用本讲的转换器。
- **若想动手**：仿照 `convert_with_tables.cpp`（`PRIORITY_TABLE` 的查表实现，用于 `sc16→sc8`）写一个新优先级的转换器，登记到一个新 id，体验「自注册 + 择优」全流程；或阅读 `gen_convert_general.py` 理解构建期代码生成如何补充 `_chdr` 的通用实现。
