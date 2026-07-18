# IdString、Const 与 hashlib

## 1. 本讲目标

前两讲（u2-l2、u2-l3）我们认识了 RTLIL 的「容器」`Design`/`Module`，以及网表的「零件」`Wire`/`Cell`/`SigSpec`。但它们身上还挂着三个更底层的东西：

- `Module`、`Wire`、`Cell` 都有名字——这些名字是什么类型？
- 每个 `Wire`、`Cell` 都带一张 `attributes` 属性表——属性的「值」是什么类型？
- `Design` 用 `modules_` 存放所有模块、`Module` 用 `wires_`/`cells_` 存放线网与单元——这些「按名字快速查找」的容器又是什么？

本讲就回答这三个问题，学完后你应当能够：

1. 理解 `RTLIL::IdString` 的**内部化（interning）**机制：为什么一个 `IdString` 在内存里其实只是一个 `int`，为什么相等比较就是整数比较，以及 `\name`（公有）与 `$name`（内部）两条命名约定的来历。
2. 读懂 `RTLIL::Const` 的字段，理解它如何用「带标签的联合体」同时表示**普通二进制常数**与**含 `x`/`z` 的四值常数**，乃至字符串常数。
3. 掌握 yosys 自研容器库 `hashlib` 中的 `dict`、`pool`、`idict`、`mfp` 的用法，并知道它们在 RTLIL 源码里的真实出场位置。

这三个原语是整个 RTLIL 的「原子」：上一层的所有结构（命名、属性、参数、查找表）都由它们搭建而成。

## 2. 前置知识

阅读本讲前，请确保你已经了解：

- **RTLIL 的内存层级**（u2-l2）：`Design` 拥有若干 `Module`，`Module` 拥有 `Wire`/`Cell`，`Design` 还承载选择栈、`scratchpad` 等横切状态。
- **网表三要素**（u2-l3）：`Wire`（线网）、`Cell`（单元实例）、`SigSpec`（信号说明）。
- **C++ 基础**：联合体（`union`）、标签联合体（tagged union）、模板、`std::vector`/`std::unordered_map` 的概念，以及「值语义」与「引用语义」的区别。

本讲涉及的几个术语先解释清楚：

- **内部化（interning）**：把所有「内容相同的字符串」在全局只存一份，并用一个整数编号代表它。之后比较两个字符串是否相同，就退化成比较两个整数——又快又能直接做哈希表的键。
- **四值逻辑**：硬件里一根线不仅有 `0`/`1`，还可能是未定义值 `x`、高阻 `z`。`Const` 必须能表达这四种（以及额外的辅助态）。
- **分离链法（separate chaining）哈希表**：哈希表的每个桶挂一条链表，冲突的元素都挂在同一桶的链上。`hashlib` 的所有容器都基于这一实现。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `kernel/rtlil.h` | 声明 `enum State`（四值/六值状态）、`enum ConstFlags`、`struct RTLIL::Const`、`struct RTLIL::IdString`，以及它们与 `hashlib` 的桥接。 |
| `kernel/rtlil.cc` | `IdString` 的全局字符串缓存（`insert`/`prepopulate`/`really_insert`）与 `Const` 的构造、转换、位运算等实现。 |
| `kernel/hashlib.h` | yosys 自研哈希容器库：`Hasher`、`hash_ops`、`dict`、`pool`、`idict`、`mfp`。 |
| `kernel/constids.inc` | 预定义的「知名 IdString」清单（`$add`、`\A` 等），启动时一次性灌入全局缓存。 |
| `backends/rtlil/rtlil_backend.cc` | `dump_const`：把 `Const` 序列化成 RTLIL 文本（`4'10xz`、`"str"`、纯十进制等），是我们观察 `Const` 行为的窗口。 |

> 说明：`IdString` 与 `Const` 的结构声明集中在 `kernel/rtlil.h`，而真正的成员函数实现写在 `kernel/rtlil.cc`。本讲引用时会把「字段」指向 `rtlil.h`、「实现」指向 `rtlil.cc`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**IdString**、**RTLIL::Const**、**hashlib 容器**。它们之间存在一条自底向上的依赖链：`hashlib` 提供容器 → `IdString` 用 `hashlib` 的思想做内部化 → `Const` 是被属性表/参数表存放的值类型 → `Module`/`Wire`/`Cell` 用 `IdString` 当名字、用 `dict<IdString, Const>` 当属性表、用 `dict<IdString, …>` 当子对象查找表。

---

### 4.1 IdString：RTLIL 的命名系统

#### 4.1.1 概念说明

在 RTLIL 里，「名字」无处不在：模块名、线网名、单元名、端口名、属性名……综合一个稍大的设计会产生成千上万个名字。如果每个名字都用一个 `std::string` 拷贝来拷贝去，既费内存又费比较时间（每次按名字查找都要做字符串比较）。

`RTLIL::IdString` 的设计动机就是**内部化**：

- 全局维护一张「字符串 ↔ 整数编号」的表。
- 每个 `IdString` 对象内部**只存一个整数 `index_`**，不存字符串本体。
- 「两个 IdString 相等」等价于「两个整数相等」，比较和哈希都退化为 `int` 运算。

此外，RTLIL 对命名有一条强约定，任何合法标识符的第一个字符只能是两种之一：

- `\`（反斜杠）：**公有标识符**，来源于用户 HDL，例如 `\counter`、`\clk`、`\out`。
- `$`（美元符）：**Yosys 内部生成**的标识符，例如 `$add`（内部加法单元）、`$auto$...`（自动生成的临时名）。

这条约定保证了「用户起的名字」与「工具生成的名字」永远不会撞车——因为它们的首字符不同。

#### 4.1.2 核心流程

`IdString` 的内部化流程可以概括为：

```
构造 IdString(str)
   │
   ▼
insert(str) ──► 在 global_id_index_ 里查 str
   │                    │
   │  已存在 ◄──────────┘  返回已有 index
   │  不存在
   ▼
really_insert(str) ──► 分配一个空闲 index，把 str 存进 global_id_storage_[index]
                        并在 global_id_index_[str] = index 建反向索引
   │
   ▼
index_ = index        （IdString 对象本身只保存这个 int）
```

要点：

1. **正负索引区分两类来源**：`index_ >= 0` 的条目放在 `global_id_storage_`（普通具名字符串）；`index_ < 0` 的条目放在 `global_autoidx_id_storage_`（自动生成的 `$auto$...` 名）。`index_ == 0` 特指空字符串 `""`。
2. **知名名字在启动时一次性预填**：`constids.inc` 里列出的常用名（`\A`、`$add` 等）在 `prepopulate()` 阶段就进入全局表，且其编号与 `StaticId` 枚举一一对应，可以**编译期直接构造**，运行期完全跳过哈希查找。
3. **比较即整数比较**：`operator==` 直接比 `index_`，`operator<` 直接比 `index_`，`hash_into` 直接哈希 `index_`。

#### 4.1.3 源码精读

**IdString 对象本体只是一个 `int`。** 看这段注释与字段：[IdString 对象只是一个 int（rtlil.h:L223-L225）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L223-L225)

```cpp
// the actual IdString object is just is a single int
int index_;
```

全局缓存由几个 `static` 成员构成：[全局字符串缓存（rtlil.h:L161-L169）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L161-L169)

```cpp
static std::vector<Storage> global_id_storage_;                 // 正索引：实际字符串
static std::unordered_map<std::string_view, int> global_id_index_;   // 字符串 → index 反查
static std::unordered_map<int, AutoidxStorage> global_autoidx_id_storage_; // 负索引：$auto$ 名
static std::unordered_map<int, int> global_refcount_storage_;   // 引用计数
static std::vector<int> global_free_idx_list_;                  // 已删除 index 的回收池
```

`insert()` 先查反查表，命中就复用，否则交给 `really_insert`：[insert：查表复用或新建（rtlil.h:L198-L212）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L198-L212)

```cpp
static int insert(std::string_view p) {
    auto it = global_id_index_.find(p);
    if (it != global_id_index_.end())
        return it->second;          // 已有，直接返回编号
    return really_insert(p, it);    // 新建
}
```

`really_insert` 强制校验首字符只能是 `$` 或 `\`，并从空闲池里取一个编号：[really_insert：校验前缀并落库（rtlil.cc:L84-L119）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L84-L119)

```cpp
log_assert(p[0] == '$' || p[0] == '\\');     // 约定：首字符只能是 $ 或 \
...
int idx = global_free_idx_list_.back();       // 复用一个回收的编号
char* buf = static_cast<char*>(malloc(p.size() + 1));
memcpy(buf, p.data(), p.size()); buf[p.size()] = 0;
global_id_storage_.at(idx) = {buf, GetSize(p)};
global_id_index_.insert(it, {std::string_view(buf, p.size()), idx});
return idx;
```

**两条命名约定在启动预填阶段就体现出来。** `prepopulate` 把 `constids.inc` 中每个名字都以前缀 `\` 写入：[prepopulate：用 \ 前缀预填知名名（rtlil.cc:L57-L67）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L57-L67)

```cpp
RTLIL::IdString::global_id_index_.insert({"", 0});   // 0 号恒为空串
RTLIL::IdString::global_id_storage_.push_back({const_cast<char*>(""), 0});
#define X(N) populate("\\" #N);
#include "kernel/constids.inc"
#undef X
```

而 `populate` 会在「以 `$` 开头的内部名」上把多余的 `\` 去掉（即内部名最终以 `$` 开头入库）：[populate：对 $ 名去掉多余前缀（rtlil.cc:L47-L55）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L47-L55)

```cpp
static void populate(std::string_view name) {
    if (name[1] == '$')            // 形如 "\$add" → 去掉前导 '\'，存成 "$add"
        name = name.substr(1);
    ...
}
```

`constids.inc` 本身是一份**必须严格按 ASCII 升序排列**的清单，这样 `ID()` 宏才能用二分查找在编译期定位知名名：[constids.inc 要求 ASCII 升序（constids.inc:L1）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L1) 以及典型条目 `X($_AND_)`：[内部单元名 $_AND_（constids.inc:L30）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L30)。

**自动生成的名字用负索引。** `NEW_ID` 宏（见 u3-l1）最终调用 `new_autoidx_with_prefix`，它递增 `autoidx` 并取负值作为索引：[new_autoidx_with_prefix：负索引（rtlil.h:L216-L221）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L216-L221)

```cpp
static IdString new_autoidx_with_prefix(const std::string *prefix) {
    int index = -(autoidx++);
    global_autoidx_id_storage_.insert({index, prefix});
    return from_index(index);
}
```

**相等、小于、哈希全部退化为整数运算**，这是内部化带来性能收益的根源：[operator== / operator< / hash_into（rtlil.h:L414-L419 与 L518）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L414-L419)

```cpp
inline bool operator<(IdString rhs) const { return index_ < rhs.index_; }
inline bool operator==(IdString rhs) const { return index_ == rhs.index_; }
...
[[nodiscard]] Hasher hash_into(Hasher h) const { return hash_ops<int>::hash_into(index_, h); }
```

`c_str()` 演示了「按需还原字符串」：正索引直接读缓存，负索引拼出 `prefix + 数字`：[c_str：按索引还原字符串（rtlil.h:L247-L264）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L247-L264)。

> **关于引用计数**：普通 `IdString` 只是「非拥有的整数视图」，不维护引用计数；`RTLIL::OwningIdString`（及其 `immortal()`）才在构造/析构时调用 `get_reference`/`put_reference` 维护全局引用计数，配合 `collect_garbage()` 回收不再被「拥有者」引用的编号。本讲你只需记住：**`IdString` 是一个轻量的、值语义的整数**。详见 [OwningIdString 的引用计数（rtlil.h:L617-L655）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L617-L655)。

#### 4.1.4 代码实践

**目标**：用 yosys 直观观察 `\` 与 `$` 两条命名约定，验证「内部名以 `$` 开头、用户名以 `\` 开头」。

**操作步骤**：

1. 准备一个最小 Verilog（示例代码，非项目原有文件）`tiny.v`：

   ```verilog
   module top(input [3:0] a, input [3:0] b, output [4:0] y);
       assign y = a + b;          // 会综合出一个内部 $add 单元
   endmodule
   ```

2. 启动 yosys 并执行：

   ```
   yosys> read_verilog tiny.v
   yosys> write_rtlil tiny.rtlil
   ```

3. 打开 `tiny.rtlil`，观察其中的标识符。

**需要观察的现象 / 预期结果**：

- 端口/线网名写作 `\a`、`\b`、`\y`（公有，`\` 开头）。
- 综合产生的加法单元名为 `$add$...\a[3:0]...\b[3:0]...` 之类（内部，`$` 开头），它的 `type` 是 `$add`。
- 这印证了「用户名带 `\`、内部名带 `$`」的约定。

> 如果无法本地构建/运行 yosys，请明确标注「待本地验证」。你也可以用源码阅读代替：在 `kernel/constids.inc` 里数一下以 `$` 开头的条目占比，体会内部单元命名规模。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `IdString::operator==` 可以只比较 `index_` 而不比较字符串本身？

> **答案**：因为内部化保证了「相同字符串 → 相同 index」。全局表里每个不同的字符串只分配一个编号，所以两个 `IdString` 内容相等当且仅当它们的 `index_` 相等。

**练习 2**：`\counter` 和 `$auto$counter$1` 在 `IdString` 内部分别由哪张表存储、`index_` 的符号是什么？

> **答案**：`\counter` 存于 `global_id_storage_`，`index_ > 0`；`$auto$counter$1` 存于 `global_autoidx_id_storage_`，`index_ < 0`。

**练习 3**：`ID($add)` 与 `ID(A)` 在 `prepopulate` 之后，存进全局表的字符串分别是什么？

> **答案**：`$add`（`populate` 去掉了 `$` 名前多余的 `\`）与 `\A`（公有名保留 `\`）。

---

### 4.2 RTLIL::Const：常数与四值逻辑

#### 4.2.1 概念说明

RTLIL 里的「常数」要同时承担多种职责：

- **位向量常数**：`parameter W = 8;`、`assign y = 4'b1010;`——每一位是 `0` 或 `1`。
- **含 `x`/`z` 的四值常数**：`assign z = 1'bz;`、`case(x) 2'b1?: ...`——位还可能是未定义 `x` 或高阻 `z`。
- **字符串常数**：`parameter MSG = "hello";`——一串字符。

`RTLIL::Const` 用一个**带标签的联合体（tagged union）**把这些表示统一起来：一个标签 `tag` 区分当前存的是「位向量」还是「字符串」，一个 `union` 同时容纳这两种表示。`flags` 字段则记录额外的语义（是否字符串、是否有符号、是否实数、是否未定宽）。

先看位状态枚举，它是「四值逻辑」的真正来源：[enum State：六种位状态（rtlil.h:L33-L40）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L33-L40)

```cpp
enum State : unsigned char {
    S0 = 0,
    S1 = 1,
    Sx = 2, // undefined value or conflict
    Sz = 3, // high-impedance / not-connected
    Sa = 4, // don't care (used only in cases)
    Sm = 5  // marker (used internally by some passes)
};
```

其中 `S0`/`S1`/`Sx`/`Sz` 是 Verilog 的四值逻辑（`0/1/x/z`），`Sa`（don't care）专用于 `casez`/`casex` 的匹配，`Sm` 是某些 pass 内部用的标记位。

语义标志 `ConstFlags` 则说明「这个常数应当如何被解释」：[enum ConstFlags（rtlil.h:L55-L61）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L55-L61)

```cpp
enum ConstFlags : unsigned char {
    CONST_FLAG_NONE    = 0,
    CONST_FLAG_STRING  = 1,
    CONST_FLAG_SIGNED  = 2,  // only used for parameters
    CONST_FLAG_REAL    = 4,  // only used for parameters
    CONST_FLAG_UNSIZED = 8,  // only used for parameters
};
```

#### 4.2.2 核心流程

`Const` 的存储模型可以画成：

```
struct Const {
    short flags;                 // 语义标志（STRING/SIGNED/REAL/UNSIZED）
    backing_tag tag;             // bits 还是 string？
    union {
        bitvectype bits_;        // = std::vector<State>，按位存四值
        std::string str_;        // 字符串，或「字节打包」的 0/1 常数
    };
}
```

构造策略决定了走哪条分支：

| 构造方式 | 存储选择 | 原因 |
| --- | --- | --- |
| `Const(std::string)` | `string`，`flags=STRING` | 真正的字符串常数 |
| `Const(long long val)` / `Const(val, width)` 且 `width` 是 8 的倍数 | `string`（字节打包） | 字节对齐时，每 8 位压成 1 字节，省内存 |
| `Const(val, width)` 且 `width` 非 8 的倍数 | `bits`（位向量） | 无法整字节打包，按位存 |
| `Const(State bit, int width)` | `bits`（位向量） | 必须按位，因为可能出现 `Sx`/`Sz` |
| `Const(std::vector<bool>)` | `bits`（位向量） | 直接按位构造 |

关键直觉：**「字节打包的 string」只是一种省内存的存储优化，它只能表示纯 `0/1`；一旦出现 `x`/`z`，就必须用位向量 `bits_`。** 当某操作需要逐位访问一个「打包成 string」的常数时，`bitvectorize_internal()` 会把它展开成 `bits_`。因此对外接口（`size()`、迭代器、`as_string()` 等）会自动处理两种底层表示的差异。

`size()` 的口径也分两种：字符串表示时 `size() = 8 * 字节数`（按位计），位向量表示时 `size() = 位数`——对外统一成「位数」。

#### 4.2.3 源码精读

**带标签联合体的字段定义**：[Const 的核心字段（rtlil.h:L1012-L1025）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1012-L1025)

```cpp
struct RTLIL::Const {
    short int flags;
private:
    using bitvectype = std::vector<RTLIL::State>;
    enum class backing_tag: bool { bits, string };
    backing_tag tag;
    union {
        bitvectype bits_;
        std::string str_;
    };
    ...
};
```

这就是「同时表示普通二进制常数与含 x/z 值」的秘密：`bits_` 是 `vector<State>`，每个 `State` 可以是 `S0/S1/Sx/Sz/Sa/Sm`；而 `str_` 是字符串（既可能是真字符串，也可能是字节打包的二进制数）。

**构造函数展示了分支策略。** 字符串构造直接置 `STRING` 标志：[Const(string)（rtlil.cc:L363-L368）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L363-L368)

```cpp
RTLIL::Const::Const(std::string str) {
    flags = RTLIL::CONST_FLAG_STRING;
    new ((void*)&str_) std::string(std::move(str));
    tag = backing_tag::string;
}
```

带宽度整数构造：宽度是 8 的倍数时按字节打包成 `string`，否则建位向量：[Const(val, width) 的两条分支（rtlil.cc:L380-L407）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L380-L407)

```cpp
if ((width & 7) == 0) {                 // 整字节 → 字节打包成 string
    new ((void*)&str_) std::string();
    tag = backing_tag::string;
    ...
} else {                                // 非整字节 → 位向量
    new ((void*)&bits_) bitvectype();
    tag = backing_tag::bits;
    for (int i = 0; i < width; i++)
        bv.push_back((val & 1) != 0 ? State::S1 : State::S0);
}
```

按位状态构造（`x`/`z` 必走这条）：[Const(State, width)（rtlil.cc:L409-L419）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L409-L419)

```cpp
RTLIL::Const::Const(RTLIL::State bit, int width) {
    ...
    new ((void*)&bits_) bitvectype();
    tag = backing_tag::bits;
    for (int i = 0; i < width; i++)
        bv.push_back(bit);              // 每一位都置成 bit，可以是 Sx/Sz
}
```

**对外口径统一。** `size()` 把两种表示都换算成「位数」：[size()（rtlil.cc:L733-L740）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L733-L740)

```cpp
int RTLIL::Const::size() const {
    if (is_str())   return 8 * str_.size();   // 字符串：字节数 × 8
    else            return bits_.size();      // 位向量：位数
}
```

**`as_string()` 把位状态映射成 RTLIL 文本字符**，正是四值逻辑外化的地方：[as_string：State→字符（rtlil.cc:L664-L679）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L664-L679)

```cpp
for (int i = sz - 1; i >= 0; --i)
    switch ((*this)[i]) {
        case S0: ret.push_back('0'); break;
        case S1: ret.push_back('1'); break;
        case Sx: ret.push_back('x'); break;
        case Sz: ret.push_back('z'); break;
        case Sa: ret += any; break;       // don't care，默认打印 '-'
        case Sm: ret.push_back('m'); break;
    }
```

**两条表示之间的转换**：当需要逐位操作一个「打包成 string」的常数时，`bitvectorize_internal` 把它展开为位向量：[bitvectorize_internal：string→bits（rtlil.cc:L751-L774）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L751-L774)。注意它是单向的（展开后丢弃字符串表示），所以叫「internal」。

`Const` 还提供一组语义谓词，便于 pass 快速判断常数性质：[is_fully_* 系列（rtlil.h:L1233-L1238）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1233-L1238)

```cpp
bool is_fully_zero() const;
bool is_fully_ones() const;
bool is_fully_def() const;      // 全 0/1，无 x/z
bool is_fully_undef() const;    // 全 x/z
bool is_fully_undef_x_only() const;
bool is_onehot(int *pos = nullptr) const;
```

最后，`Const` 也实现了 `hash_into`（[rtlil.h:L1258](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1258)），因此它可以作为 `hashlib` 容器的值，甚至作为键。

#### 4.2.4 代码实践

**目标**：用 RTLIL 文本后端观察 `Const` 如何序列化不同类型的常数，验证「四值逻辑、字符串、纯整数」三种表示。

**操作步骤**：

1. 写一个带各种常数的 Verilog（示例代码）`consts.v`：

   ```verilog
   module top(output [3:0] a, output [31:0] b);
       assign a = 4'b10xz;        // 含 x/z 的 4 位常数
       assign b = 5;              // 32 位整数
       parameter MSG = "hi";      // 字符串
   endmodule
   ```

2. 运行：

   ```
   yosys> read_verilog consts.v
   yosys> write_rtlil consts.rtlil
   ```

3. 打开 `consts.rtlil`，定位 `connect` 与 `parameter` 行。

**需要观察的现象 / 预期结果**（对照 `dump_const` 的实现，见下文链接）：

- `4'b10xz` 会输出形如 `4'10xz`（位串里直接出现 `x`/`z`）。它不是 32 位、且含非 0/1 态，所以走位串分支。
- `5`（32 位纯整数）会被打印成纯十进制 `5`——**仅当宽度恰好为 32 且无 `x/z` 时**，`dump_const` 才走 `autoint` 十进制快捷分支；这正是为什么我们要把它声明成 `[31:0]`。换成 `4'd5` 则会输出 `4'0101`。
- 字符串 `"hi"` 会带引号输出（`CONST_FLAG_STRING` 走 `"..."` 分支）。
- 序列化规则全部来自 `dump_const`：[dump_const：常数的文本序列化（rtlil_backend.cc:L44-L104）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L44-L104)。重点看其中 `width == 32 && autoint` 的十进制分支（L49-L63）、对 `State::Sx/Sz` 输出 `'x'/'z'`（L78-L79），以及对 `CONST_FLAG_STRING` 走 `"..."` 分支（L85-L103）。

> 若无法运行，标注「待本地验证」；可改为阅读型实践：阅读 `dump_const` 的 `switch(data[i])`，说明为什么「全 x」会单独走 `f << "x"`（提示：`is_fully_undef_x_only()`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Const(5, 8)` 用 `string`（字节打包）存储，而 `Const(State::Sx, 4)` 必须用位向量？

> **答案**：`5` 占 8 位且无 `x/z`，可整字节打包（1 字节）省内存；`Sx` 含未定义值，字节打包只能存 `0/1` 无法表达 `x`，所以必须用 `vector<State>` 位向量。

**练习 2**：一个 `tag == string` 但 `flags` 不含 `CONST_FLAG_STRING` 的 `Const`，表示什么？

> **答案**：它表示一个**纯 0/1 的位向量常数**，只是因为位宽恰好是 8 的倍数而被「字节打包」存进 `str_`，并非真正的字符串。`decode_string()` / `as_bool()` 等方法会按字节解释它。

**练习 3**：`Const::size()` 对字符串表示返回 `8 * str_.size()`，这说明了什么口径？

> **答案**：`size()` 对外统一返回**位数**而非字节数或字符数。即便是字符串常数，它的「位宽」也按 8 乘字节数计算。

---

### 4.3 hashlib：yosys 自研的高性能容器

#### 4.3.1 概念说明

标准库的 `std::unordered_map`/`unordered_set` 虽可用，但 yosys 出于**性能、内存与可控性**的考虑，自研了一套哈希容器 `hashlib`，包含 `dict`、`pool`、`idict`、`mfp`。RTLIL 几乎所有「按名查找」的结构都用它们。`hashlib` 不依赖 yosys 的其他部分，可以单独使用（但官方建议通过 `kernel/yosys_common.h` 间接包含，详见 [hashlib.h:L30-L56 的说明](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L30-L56)）。

四个容器各司其职：

| 容器 | 对应标准库概念 | 用途 |
| --- | --- | --- |
| `dict<K, T>` | `unordered_map<K,T>` | 键值映射 |
| `pool<K>` | `unordered_set<K>` | 键集合（只关心在不在） |
| `idict<K>` | 「键 ↔ 整数下标」双向表 | 把任意键内部化为连续整数下标 |
| `mfp<K>` | 并查集（merge/find/promote） | 等价类归并，例如 `SigMap` 的信号归一化（见 u3-l2） |

它们共享同一套实现：**分离链法**，桶数组用 `std::vector<int>`（存的是 `entries` 的下标，而不是指针），数据用 `std::vector<entry_t>` 连续存储。这种「用整数索引代替指针」的设计对缓存友好。

#### 4.3.2 核心流程

以 `dict` 为例，核心数据结构与一次查找：

```
dict<K,T> 内部：
  hashtable : vector<int>     // 桶数组，每桶存 entries 的下标（链头），空桶为 -1
  entries   : vector<entry_t> // 连续存储 {pair<K,T> udata; int next;}
  ops       : hash_ops<K>     // 提供 hash() 与 cmp()

查找 operator[](key) / find(key)：
  hash = ops.hash(key) % hashtable.size()
  i = hashtable[hash]                  // 链头
  while (i >= 0 && !ops.cmp(entries[i].udata.first, key))
      i = entries[i].next              // 沿链找
  命中 → entries[i].udata.second
```

**动态扩容**采用素数桶大小，并在装载因子超过阈值时 `rehash`：桶容量取自一张预计算的素数表 [hashtable_size：素数桶大小（hashlib.h:L350-L376）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L350-L376)。装载因子阈值由两个常量控制：`hashtable_size_trigger = 2`、`hashtable_size_factor = 3`，即当 `entries.size() * 2 > hashtable.size()` 时扩容到约 `3 × capacity` 大小（[hashlib.h:L64-L65](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L64-L65)）。

**哈希与比较的统一抽象**是 `hash_ops<T>`：默认实现通过 SFINAE 自动适配整数、枚举、指针、`std::string`、`pair`/`tuple`/`vector` 以及任何带 `hash_into` 方法的类型。这正是 `IdString` 和 `Const` 能直接当键用的原因——它们都提供了 `hash_into`。

#### 4.3.3 源码精读

**`hash_ops` 的自动派发**——整型走 `hash32/hash64`，字符串按 8 字节块吞入，其他类型调用其 `hash_into`：[hash_ops 主模板（hashlib.h:L159-L195）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L159-L195)

```cpp
template<typename T> struct hash_ops {
    static inline bool cmp(const T &a, const T &b) { return a == b; }
    static inline Hasher hash_into(const T &a, Hasher h) {
        if constexpr (std::is_integral_v<T>) {
            ...                       // 整型：hash32/hash64
        } else if constexpr (std::is_same_v<T, std::string>) {
            ...                       // 字符串：按 8 字节块
        } else {
            return a.hash_into(h);    // 其他：委托类型自身的 hash_into
        }
    }
};
```

哈希算法本身是 DJB2 变种（`HasherDJB32`），用 xorshift 做位混合：[HasherDJB32（hashlib.h:L90-L146）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L90-L146)。其核心 `hash32` 满足

\[
\text{state} \leftarrow \text{xorshift}\big(\text{fudge} \oplus \text{djb2\_xor}(i,\,\text{state})\big)
\]

**`dict` 的内部结构**——桶数组 + 连续 entries + 链：[dict 的两个字段（hashlib.h:L423-L424）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L423-L424)

```cpp
std::vector<int> hashtable;
std::vector<entry_t> entries;
```

`operator[]` 是「找不到就插入」的标准映射语义：[dict::operator[]（hashlib.h:L823-L830）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L823-L830)

```cpp
T& operator[](const K &key) {
    Hasher::hash_t hash = do_hash(key);
    int i = do_lookup(key, hash);
    if (i < 0)
        i = do_insert(std::pair<K, T>(key, T()), hash);
    return entries[i].udata.second;
}
```

**`pool` 是「只存键」的集合**，其 `operator[]` 返回 `bool`（表示是否存在）而非引用，这是它和 `dict` 最直观的区别：[pool::operator[] 返回 bool（hashlib.h:L1202-L1207）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L1202-L1207)

```cpp
bool operator[](const K &key) {
    Hasher::hash_t hash = do_hash(key);
    int i = do_lookup(key, hash);
    return i >= 0;
}
```

`pool::insert` 返回 `(iterator, 是否新增)`：[pool::insert（hashlib.h:L1130-L1138）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L1130-L1138)。

**`idict` 把键内部化为连续整数下标**，正向 `operator()(key)` 返回下标（不存在则插入），反向 `operator[](int)` 由下标取键：[idict::operator() 正向（hashlib.h:L1296-L1303）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L1296-L1303) 与 [idict::operator[] 反向（hashlib.h:L1343-L1346）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L1343-L1346)。`idict` 内部就是一个 `pool` 作数据库（[hashlib.h:L1268](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L1268)）。

**`mfp` 是并查集**，提供 `merge`/`find`/`promote`，用原子量支持并发安全的 `ifind`：[mfp 类声明（hashlib.h:L1368-L1382）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L1368-L1382)。u3-l2 中 `SigMap` 正是用 `mfp<SigBit>` 把连通的信号位归并到唯一代表位。

**这些容器在 RTLIL 里的真实出场**，是最好的「典型用法」示范：

- 属性表（`Wire`/`Cell`/`Module` 都继承自 `AttrObject`）：[attributes 字段（rtlil.h:L1263）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1263)
  ```cpp
  dict<RTLIL::IdString, RTLIL::Const> attributes;
  ```
- `Design` 的模块表：[modules_ 字段（rtlil.h:L1904）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1904)
  ```cpp
  dict<RTLIL::IdString, RTLIL::Module*> modules_;
  ```
- `Module` 的线网表与单元表：[wires_/cells_（rtlil.h:L2077-L2078）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2077-L2078)
  ```cpp
  dict<RTLIL::IdString, RTLIL::Wire*> wires_;
  dict<RTLIL::IdString, RTLIL::Cell*> cells_;
  ```
- `Selection` 中被选中的模块集合（`pool` 用法）：[selected_modules（rtlil.h:L1785）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1785)
  ```cpp
  pool<RTLIL::IdString> selected_modules;
  ```

可以看到一条贯穿全篇的逻辑链：**名字是 `IdString`（一个 int）→ `hash_ops<IdString>` 直接哈希这个 int → `dict<IdString, …>` 用它当键做 O(1) 查找**。这也是 `IdString` 内部化的最终回报。

为了让 `hashlib` 知道「`IdString` 怎么哈希/比较」，源码在 `rtlil.h` 末尾为它特化了 `hash_ops`：[hash_ops<IdString> 特化（rtlil.h:L658-L670）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L658-L670)。

#### 4.3.4 代码实践

**目标**：在真实源码里找出 `dict` 与 `pool` 的典型用法，并归纳它们的 API 模式。

**操作步骤**（源码阅读型实践）：

1. 在 `kernel/rtlil.h` 中定位上面列出的四处字段（`attributes`、`modules_`、`wires_`/`cells_`、`selected_modules`）。
2. 在 `kernel/rtlil.cc` 或 `kernel/rtlil.h` 中搜索这些字段是如何被访问的，例如：
   - `module(name)` / `wire(name)` / `cell(name)` → 内部多半是 `dict::at()` 或 `dict::find()`。
   - `addWire`/`addCell` → 内部多半是 `dict::operator[]` 或 `emplace` 赋值。
3. 归纳 `dict` 与 `pool` 的典型 API：`count(k)`、`find(k)`、`operator[](k)`、`erase(k)`、`at(k)`、迭代 `for (auto &it : d) { it.first; it.second; }`。

**需要观察的现象 / 预期结果**：

- `dict` 的迭代器解引用得到 `std::pair<K,T>&`，因此 `it->first`、`it->second`。
- `pool` 的迭代器解引用直接得到键 `K&`。
- `pool[k]` 返回 `bool`，`dict[k]` 返回值引用——这是二者最易混的点。

> 进阶（可选）：阅读 `dict::do_rehash`（[hashlib.h:L443-L454](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/hashlib.h#L443-L454)），说明为什么桶大小要取素数、为什么用「entries 下标」而不是指针做链。

#### 4.3.5 小练习与答案

**练习 1**：`pool<IdString> s; s["\\a"];` 与 `dict<IdString, int> d; d["\\a"];` 各自的返回类型和行为是什么？

> **答案**：`s["\\a"]` 返回 `bool`，表示 `"\\a"` 是否在集合中（注意它**不会**插入）；`d["\\a"]` 返回 `int&`，若键不存在会**插入**一个默认值 `0` 并返回其引用。

**练习 2**：为什么 `IdString` 能直接当作 `dict`/`pool` 的键，而不需要手写哈希函数？

> **答案**：因为 `rtlil.h` 为 `IdString` 特化了 `hash_ops`，且 `IdString` 本身的 `hash_into` 直接哈希其 `index_`（一个 int）。`hashlib` 的容器通过 `hash_ops<K>` 统一获取哈希与比较，自动生效。

**练习 3**：`idict` 与 `dict` 的根本区别是什么？它解决了什么问题？

> **答案**：`dict` 是通用键值映射；`idict` 把每个键内部化为一个**连续的小整数下标**（0,1,2,…），可双向查找。它用于「需要把任意键当作数组下标」的场景——这正是 `IdString` 内部化思想在容器层面的对应物（事实上 `IdString` 的全局表就扮演了类似 `idict` 的角色）。

---

## 5. 综合实践

把三个原语串起来，完成一个**「在源码与运行时两端互相印证」**的小任务。

**任务背景**：`Wire`/`Cell` 的属性表是 `dict<IdString, Const>`，即「名字 → 常数」的映射。请你解释清楚这一行声明里每个类型扮演的角色，并用 yosys 验证属性值在 RTLIL 文本里的样子。

**步骤**：

1. **源码侧**。打开 [AttrObject::attributes（rtlil.h:L1263）](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1263)，回答：
   - 键类型 `RTLIL::IdString` 为何能高效地做哈希键？（引用 4.1 的内部化机制：它本质是个 int，`hash_ops<IdString>` 直接哈希 `index_`。）
   - 值类型 `RTLIL::Const` 如何同时表示「整数属性」和「含 `x`/`z` 的属性」？（引用 4.2：`tag` 区分 `bits`/`string`，`bits_` 是 `vector<State>`，`State` 含 `Sx/Sz`。）

2. **运行时侧**。写一个带属性的设计（示例代码）`attr.v`：

   ```verilog
   module top;
       (* my_int = 42 *)
       (* my_undef = 4'bxx *)
       wire [3:0] w;
   endmodule
   ```

   执行 `read_verilog attr.v` 后 `write_rtlil attr.rtlil`，观察 `wire` 行上 `attribute` 的写法：整数属性如何打印、含 `x` 的属性如何打印，并把它们对应回 `Const` 的 `flags` 与 `State`。

3. **归纳**。用一段话总结：从「用户写了 `(* my_int = 42 *)`」到「属性表里多出一项 `attributes[ID::my_int] = Const(42)`」，经历了哪些类型（`IdString` 作为属性名、`Const` 作为属性值、`dict` 作为容器）。

**预期结果**：你能用自己的话说清「RTLIL 的命名、常数、容器」三件套如何协作，并能指出它们各自在 `rtlil.h` 中的字段位置。若某步无法本地运行，请标注「待本地验证」。

## 6. 本讲小结

- `RTLIL::IdString` 是**内部化字符串**：对象本体只是一个 `int index_`，相等/比较/哈希都退化为整数运算；`\` 开头是公有名、`$` 开头是内部名，负索引专供 `$auto$` 自动名。
- `RTLIL::Const` 用**带标签联合体**统一三种常数：`bits_`（`vector<State>`，可表达 `0/1/x/z`）、`str_`（字符串或字节打包的 0/1 数）、以及 `flags` 携带的语义；`size()` 对外统一为「位数」。
- `State` 枚举（`S0/S1/Sx/Sz/Sa/Sm`）是四值逻辑与若干辅助态的根源；`is_fully_*` 系列是 pass 常用的快速谓词。
- `hashlib` 自研 `dict`/`pool`/`idict`/`mfp`，采用分离链法 + 素数桶 + 整数下标链，是 RTLIL 所有按名查找结构的底层容器。
- 三者构成一条逻辑链：`IdString`（int）→ `hash_ops` 直接哈希 int → `dict<IdString, …>`（如 `attributes`、`modules_`、`wires_`、`cells_`）和 `pool<IdString>`（如 `selected_modules`）。

## 7. 下一步学习建议

- **回到上层结构**：现在你已经掌握了「原子」，建议重读 u2-l2、u2-l3 与 u3-l1，体会 `Design::modules_`、`Module::wires_`/`cells_`、`AttrObject::attributes` 是如何建立在这三个原语之上的。
- **内部单元命名**：下一类关键知识是 u3-l4「Yosys 内部单元库」，那里会系统讲解 `$and`/`$or`/`$mux`/`$dff`/`$mem` 等 `$` 开头单元的端口与参数——你现在已知这些单元名本身就是 `IdString`、其参数值就是 `Const`。
- **进阶阅读**：
  - 官方文档 `docs/source/yosys_internals/hashing.rst`，了解 `hashlib` 的设计取舍（`hashlib.h` 头部多处引用它）。
  - `kernel/rtlil.cc` 中 `Const` 的运算函数（`as_int`、`convertible_to_int`、`compress` 等），理解常数如何在 pass 间被消费。
  - `OwningIdString::collect_garbage`（[rtlil.cc:L240](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L240) 起），了解内部化表的回收机制。
