# Simple Packed Serialization 原理

## 1. 本讲目标

在上一讲（u5-l1）里，你已经看到 wrapper function 把跨进程调用收敛成「字节进、字节出」——两端只搬运一段字节，通信层绝不解释字节含义。但这段字节**从哪来、又怎么还原成 C++ 对象**？这正是本讲的职责：SPS（Simple Packed Serialization，简单紧凑序列化）给出了一套「把任意 C++ 值打包成一串小端字节、再原样还原回来」的方案，是 wrapper function 之上那层「类型还原」的具体实现。

读完本讲，你应该能够：

1. 说出 SPS 的**紧凑 wire 格式**：原语一律小端原样存放、序列写作「uint64 长度 + 逐元素」、元组写作「逐元素、无任何填充」。
2. 解释 **`SPSSerializationTraits<SPSTagT, ConcreteT>`** 这张「标签 → 具体类型」映射表为什么需要三件套 `size / serialize / deserialize`，并理解**两阶段**（先算大小、再写内容）为何不能合并。
3. 认识 `SPSTuple` / `SPSSequence` / `SPSString` / `SPSMap` / `SPSOptional` 这组**标签类型**，以及 `SPSArgList` 这个把它们粘起来的变参工具。
4. 能为**自定义结构体**特化 `SPSSerializationTraits`，让 SPS 懂得如何序列化它，并写一个往返（round-trip）单元测试。

---

## 2. 前置知识

本讲承接 u5-l1，这里只做最短回顾，不重复其细节：

- **wrapper function 的字节容器**：`orc_rt::WrapperFunctionBuffer` 是一段可跨进程搬运的字节缓冲（RAII、仅移动）。SPS 产出的字节就装进它、也从它读出。
- **通信层只搬字节、不解释含义**：所以「这个 uint32 到底是 42 还是一组位标志」「这串 char 是 int 数组还是字符串」——这些类型含义只能由 SPS 这一**应用层**来约定，两端必须用同一套「标签」。
- **C++ 模板与特化**：本讲大量使用「主模板 + 偏特化」的手法。如果你对 `template <> class Foo<...> { ... };` 这种「为某个具体类型提供专属实现」的写法感到陌生，建议先回顾 C++ 模板特化。
- **小端序（little-endian）**：低位字节存放在低地址。 orc-rt 几乎只跑在小端机（x86、ARM 小端模式）上，但 SPS 的代码在**大端机**上会做字节翻转，以保证 wire 格式**永远是小端**。

一个本讲独有的关键概念先放在这里，后面反复用到：

- **标签（tag）与具体类型（concrete type）的分离**。SPS 的序列化 trait 写作 `SPSSerializationTraits<SPSTagT, ConcreteT>`，左边是「线上长什么样」的**标签**，右边是「内存里是什么 C++ 类型」的**具体类型**。二者不必相同：例如标签 `SPSString`（一串 char）既能映射到 `std::string`，也能映射到 `std::string_view`；标签 `SPSExecutorAddr`（一个 uint64）既能映射到 `ExecutorAddr`，也能映射到任意 `T*`。这种「一个标签、多个宿主类型」的解耦是 SPS 表达力的核心。

---

## 3. 本讲源码地图

本讲围绕一个纯头文件展开，配两个测试文件：

| 文件 | 作用 |
|------|------|
| [`include/orc-rt/SimplePackedSerialization.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h) | **本讲主角**：定义 `SPSOutputBuffer` / `SPSInputBuffer`、`SPSSerializationTraits`、`SPSArgList`，以及 `SPSTuple` / `SPSSequence` / `SPSString` / `SPSMap` / `SPSOptional` 等全部标签类型与它们的 wire 格式。 |
| [`test/unit/SimplePackedSerializationTest.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTest.cpp) | **测试**：覆盖缓冲读写、各类原语、序列、元组、可选、地址、错误/期望值的往返。是理解每种标签 wire 行为的「试金石」。 |
| [`test/unit/SimplePackedSerializationTestUtils.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTestUtils.h) | **测试工具**：提供 `spsSerialize` / `spsDeserialize` / `blobSerializationRoundTrip` 三个模板函数，把「算大小→分配→序列化→反序列化→比对」的样板封装起来。 |
| [`include/orc-rt/bit.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/bit.h) | （支撑）提供 `endian::native` 与 `byteswap`，原语序列化用它来保证「永远写小端」。 |

> 提示：测试文件名与被测源码基本一一对应（详见 u1-l3 的「反向定位源码」结论）。读不懂某个 trait 的行为时，直接去同名测试里找用例，往往一句话就明白了。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，自底向上：

1. **字节缓冲** `SPSOutputBuffer` / `SPSInputBuffer`——序列化的物理地基。
2. **序列化契约** `SPSSerializationTraits` + `SPSArgList`——所有标签类型共同遵守的「三件套 + 变参粘合」机制。
3. **标签类型与 wire 格式** `SPSTuple` / `SPSSequence` / `SPSString` / `SPSMap` / `SPSOptional` 等——一套可组合的「线上词汇」。

### 4.1 字节缓冲：SPSOutputBuffer / SPSInputBuffer

#### 4.1.1 概念说明

序列化就是把一个 C++ 值变成一串 `char`；反序列化反之。SPS 用两个极简的包装类来表示「这串 char」：

- `SPSOutputBuffer`：一段**可写**的字符缓冲，带**溢出检查**。`write(data, size)` 在剩余空间不够时直接返回 `false`，绝不高估、绝不越界。
- `SPSInputBuffer`：一段**只读**的字符缓冲，带**下溢检查**。`read(data, size)` 在剩余字节不够时返回 `false`。

它们不分配内存、不持有所有权，只是「拿着一段 `char*` + 长度」往前推进的游标（cursor）。所有内存由调用方负责（通常是上一讲的 `WrapperFunctionBuffer`，或测试里 `std::make_unique<char[]>`）。这种「零所有权」设计让缓冲可以套在任意已分配好的内存之上，非常轻量。

#### 4.1.2 核心流程

两个缓冲的用法都是「顺序推进、用尽即止」：

```text
序列化（写）:  调用方先算出总大小 -> 分配 char[size] -> 用 SPSOutputBuffer 包住 -> 依次 write 每个字段 -> 写满即成功
反序列化（读）: 用 SPSInputBuffer 包住收到的字节 -> 依次 read 每个字段 -> 读到末尾即成功
```

关键不变量：

- `write` / `read` 一旦返回 `false`，整个序列化就**短路失败**——上层 trait 把 `false` 一路冒泡上去，最终让这次 wrapper 调用失败。这正是「带外错误」要表达的「反序列化失败」短路路径（见 u5-l2）。
- 每次成功 `write` / `read` 后，内部游标 `Buffer` 前进、`Remaining` 减少，保证下一个字段紧接上一个字段，**中间没有任何填充字节**（packed 的由来）。

#### 4.1.3 源码精读

`SPSOutputBuffer` 只有两个成员和一个 `write`：
[SimplePackedSerialization.h:57-73](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L57-L73) —— 定义可写字节缓冲，`write` 在 `Size > Remaining` 时返回 `false`，否则 `memcpy` 后推进游标。

`SPSInputBuffer` 多了两个为「零拷贝」服务的成员 `data()` 与 `skip()`：
[SimplePackedSerialization.h:76-102](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L76-L102) —— `read` 把字节拷出去；`data()` 返回「当前游标所指位置」，`skip(n)` 仅前进不拷贝。这两个方法合起来，让 `string_view`、`span<const char>` 这类「只读视图」能**直接指向输入缓冲内部**，而不必复制。

测试用最朴素的方式验证这两个游标：
[SimplePackedSerializationTest.cpp:20-38](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTest.cpp#L20-L38) —— 往 8 字节缓冲里逐字节写，写满 8 个成功、第 9 个返回 `false`，并断言内容按序写入。
[SimplePackedSerializationTest.cpp:40-51](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTest.cpp#L40-L51) —— 从 8 字节缓冲里逐字节读，读尽后再次 `read` 返回 `false`。

#### 4.1.4 代码实践

1. **目标**：亲手感受两个游标的「推进」与「用尽即止」。
2. **步骤**：阅读上面两个测试用例 `SPSOutputBuffer` / `SPSInputBuffer`（行号见上），理解断言为何成立。然后在测试文件里**新增**一个微型用例：分配 4 字节缓冲，先 `write` 一个 `int32_t`（4 字节，成功），再 `write` 一个 `char`（应失败），断言第二次返回 `false`。
3. **现象**：第一个 `write` 后 `Remaining` 归零，第二个 `write` 因 `Size > Remaining` 立即返回 `false`。
4. **预期结果**：两个断言（第一次成功、第二次失败）通过。
5. **待本地验证**：本实践需编译并运行 `check-orc-rt-unit`，结果以本地为准。

#### 4.1.5 小练习与答案

**练习**：`SPSInputBuffer::skip(n)` 与 `read(buf, n)` 都会消费 n 个字节，它们的区别是什么？为什么 SPS 同时需要这两个？

**参考答案**：`read` 把 n 个字节**拷贝**到调用方提供的缓冲里；`skip` 只把游标**前进** n 个字节、不拷贝任何东西。同时需要两者，是因为有些反序列化目标（如 `std::string`、`std::vector`）需要把字节**复制**进自己的存储，而有些只读视图（`std::string_view`、`span<const char>`）只需**指向**输入缓冲内部——后者用 `data()` 记下起始位置、再用 `skip()` 越过数据即可，零拷贝。这正是源码里 `string_view` 反序列化的写法（见 4.3.3）。

---

### 4.2 序列化契约：SPSSerializationTraits 与 SPSArgList

#### 4.2.1 概念说明

缓冲只管搬字节；**怎么把一个具体 C++ 值搬成字节**，由一张「映射表」规定。SPS 用一个主模板 `SPSSerializationTraits<SPSTagT, ConcreteT>` 来表示这张表，并为每种「标签 + 具体类型」组合提供一个**偏特化**。每个特化必须实现三个静态方法，构成 SPS 的核心契约：

| 方法 | 职责 | 返回 |
|------|------|------|
| `size(Value)` | 这个值序列化后占**多少字节** | `size_t` |
| `serialize(OB, Value)` | 把值**写进**输出缓冲 | `bool`（成功/失败） |
| `deserialize(IB, Value)` | 从输入缓冲**读回**到值 | `bool`（成功/失败） |

为什么是三件套、而不是一个 `toBytes()`？因为 SPS 采用**两阶段**序列化：

```text
阶段 1（size）   : 先算出总字节数，据此一次性分配恰好大小的缓冲。
阶段 2（serialize）: 再把内容写进这块缓冲。
```

这种「先量后写」的好处是**精确分配、无需动态扩容**——缓冲可以复用 wrapper function 那块定长内存（u5-l1）。代价是值会被「访问两次」：一次算大小、一次真正写出。这正是后面 `Error` 这种「只能消费一次」的类型需要特殊包装（`SPSSerializableError`）的根本原因。

> 注意：`SPSTagT` 与 `ConcreteT` 通常**不是**同一个类型。`SPSTagT` 描述线上格式（一种「schema」），`ConcreteT` 是内存里的 C++ 类型。唯一的例外是原语——对于 `int32_t` 等，标签和具体类型恰好都是 `int32_t`（见 4.2.3）。

#### 4.2.2 核心流程

`SPSArgList<Tag1, Tag2, ...>` 是把「多个字段的序列化」粘合起来的变参工具，它把整列字段递归地交给每个字段各自的 `SPSSerializationTraits`：

```text
SPSArgList<int32_t, SPSString>::serialize(OB, 42, "foo")
  = SPSSerializationTraits<int32_t, int32_t>::serialize(OB, 42)   // 先写 int32
  && SPSSerializationTraits<SPSString, std::string>::serialize(OB, "foo")  // 再写 string
```

`size` 与 `deserialize` 同理递归。整列用 `&&` 短路串联：任一字段失败，整列立即失败。标签类型（`SPSTuple`、`SPSSequence`、`SPSOptional`）几乎都**不自己实现**三件套，而是转交给 `SPSArgList`——`SPSTuple` 甚至只内嵌一个 `typedef ... AsArgList` 就够了（见 4.3.3）。这是 SPS「组合优先」的设计哲学：标签只描述结构，真正干活的是 `SPSArgList` + 各字段的 `SPSSerializationTraits`。

#### 4.2.3 源码精读

主模板只有声明、无定义——逼着你为每种组合提供特化：
[SimplePackedSerialization.h:104-107](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L104-L107) —— 「特化此模板以描述某具体类型的序列化/反序列化」。没有特化的组合会编译期报「incomplete type」错。

`SPSArgList` 用经典的「空表 + 非空表（头 + 尾）」递归特化实现：
[SimplePackedSerialization.h:112-143](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L112-L143) —— 非空表的 `size/serialize/deserialize` 各自处理「首元素」，再用 `&&`（或 `+`）递归处理「剩余元素」。

原语特化是「标签 == 具体类型」的唯一情形，用 SFINAE 把它限定在整数/bool/char 集合内，并强制小端：
[SimplePackedSerialization.h:145-174](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L145-L174) —— `size` 就是 `sizeof`；`serialize` 在大端机上先 `byteswap` 再写、小端机直接写；`deserialize` 读出后同样按需翻转。这保证了 wire 格式**永远是小端**，与两端机器字节序无关。

`byteswap` 与 `endian::native` 的定义在支撑头里：
[bit.h:53-107](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/bit.h#L53-L107) —— `endian::native` 在大端机上取 `big`，据此触发原语特化里的翻转；`byteswap` 按 `sizeof(T)` 分派到各档实现。

测试工具 `blobSerializationRoundTrip` 把「两阶段」流程封装得一览无余：
[SimplePackedSerializationTestUtils.h:34-52](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTestUtils.h#L34-L52) —— 先 `BST::size(Value)` 算大小、`make_unique<char[]>` 分配、用 `SPSOutputBuffer` 包住再 `serialize`，接着换 `SPSInputBuffer` 包住同一段内存 `deserialize` 出新值，最后用比较器断言「值相等」。这就是 SPS 的「round-trip」标准范式。

#### 4.2.4 代码实践

1. **目标**：用 `SPSArgList` 单独序列化一个「bool + int32 + string」的参数列，亲手走一遍两阶段。
2. **步骤**：参照 `ArgListSerialization` 测试用例
   [SimplePackedSerializationTest.cpp:224-248](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTest.cpp#L224-L248)，用 `using BAL = SPSArgList<bool, int32_t, SPSString>;`，依次调用 `BAL::size(true, 42, "foo")` 分配缓冲、`BAL::serialize(OB, ...)` 写入、再 `BAL::deserialize(IB, out1, out2, out3)` 读回，最后断言三个输出值与输入一致。
3. **现象**：`BAL::size` 返回 \(1 + 4 + (8 + 3) = 16\) 字节（bool 占 1、int32 占 4、字符串「foo」= 8 字节长度 + 3 字符）。
4. **预期结果**：三个字段值经往返后完全相等，测试通过。
5. **待本地验证**：以本地 `check-orc-rt-unit` 运行结果为准。

#### 4.2.5 小练习与答案

**练习 1**：`SPSSerializationTraits<SPSExecutorAddr, T*>`（任意指针）与 `SPSSerializationTraits<SPSExecutorAddr, ExecutorAddr>` 共用同一个标签 `SPSExecutorAddr`，但具体类型不同。这体现了 SPS 哪个设计原则？带来什么好处？

**参考答案**：体现了「标签与具体类型分离、一个标签可对应多个宿主类型」的原则。好处是线上格式统一（一律是一个 uint64 地址），但内存侧既能用类型安全的 `ExecutorAddr`，也能直接用裸指针 `T*`——调用方按自己手头的类型写代码，无需手动转换。

**练习 2**：为什么 `Error` 不能直接特化 `SPSSerializationTraits`，而要绕一层 `SPSSerializableError`？

**参考答案**：因为两阶段序列化需要**访问值两次**（一次 `size`、一次 `serialize`），但 `orc_rt::Error` 是**仅移动、且只能消费一次**的（详见 u2-l3、u9-l2）：一旦取出错误信息它就失效了，没法再 `serialize` 一遍。`SPSSerializableError` 在构造时就把 `Error` 转成可重复读取的字符串缓存起来，从而能被 `size` 与 `serialize` 各看一次。`SPSExpected` / `SPSSerializableExpected` 同理。

---

### 4.3 标签类型与 wire 格式

#### 4.3.1 概念说明

掌握了缓冲与契约，剩下的就是「SPS 内置了哪些标签、它们各自线上长什么样」。这一节是 SPS 的「词汇表」。源码文件头部的注释把整套格式写得清清楚楚：
[SimplePackedSerialization.h:15-31](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L15-L31) —— 规定原语为小端二补码、序列为「uint64 长度 + 逐元素」、元组为「逐元素无填充」。注意末尾强调：**orc-rt 端的这套行为必须与控制端 `llvm/ExecutionEngine/Orc/Shared/WrapperFunctionUtils.h` 保持一致**——因为两端要互通，wire 格式是一份跨仓库的契约。

下表汇总常用标签及其 wire 格式：

| 标签 | 典型具体类型 | wire 格式 |
|------|--------------|-----------|
| `int32_t` 等原语 | 同名原语 | 小端原样，`sizeof` 字节；`bool` 为 1 字节（0/1） |
| `SPSSize` | `size_t` | 恒为 8 字节（uint64），反序列化时做 32 位平台溢出检查 |
| `SPSSequence<E>` | `std::vector<T>` / `span<T>` | uint64 长度 + 逐元素（无填充） |
| `SPSString`（=`SPSSequence<char>`） | `std::string` / `std::string_view` | uint64 长度 + 原始字符 |
| `SPSMap<K,V>`（=`SPSSequence<SPSTuple<K,V>>`） | `std::unordered_map<K,V>` | uint64 项数 + 逐个 (key,value) |
| `SPSTuple<T1..Tn>` | `std::tuple` / `std::pair` / 自定义结构体 | 逐元素（无填充） |
| `SPSOptional<T>` | `std::optional<T>` | 1 字节 bool（有/无）+ 若有则紧跟 `T` |
| `SPSExecutorAddr` | `ExecutorAddr` / `T*` | uint64 地址值 |

> 设计要点——**「无填充（packed）」**。SPS 不为对齐插入任何 padding，这让 wire 体积最小，代价是字段不必按自然边界对齐。对跨进程 RPC，省带宽比对齐访问更重要。

#### 4.3.2 核心流程

以「一个元组 `<int32_t, int32_t, SPSString>` 取值 `(10, -5, "hi")`」为例，手算它的 wire 字节。设小端机，各字段按顺序拼接、无填充：

- `int32_t 10` → \(0x0000000A\) → 小端 `0A 00 00 00`
- `int32_t -5` → \(0xFFFFFFFB\) → 小端 `FB FF FF FF`
- 字符串长度 2 → uint64 小端 `02 00 00 00 00 00 00 00`，再跟两字节 `68 69`（"hi"）

总字节数：

\[
\text{size} = \underbrace{4}_{10} + \underbrace{4}_{-5} + \underbrace{(8+2)}_{\text{"hi"}} = 18
\]

通用公式：序列 `SPSSequence<E>` 的 wire 体积为

\[
\text{size}\bigl(\text{Sequence}\langle E\rangle\bigr) \;=\; 8 \;+\; \sum_{i=1}^{n}\text{size}(e_i)
\]

元组与序列都靠 `SPSArgList` 递归落地——`SPSSequence` 的特化先写一个 uint64 长度，再用 `for` 循环逐元素调 `SPSArgList<E>`；`SPSTuple` 对 `std::tuple`/`std::pair` 的特化则把整列字段交给 `SPSTuple<...>::AsArgList`。

#### 4.3.3 源码精读

标签类型本身大多是「前向声明 + 一行 using」的轻量骨架，真正逻辑在特化里：

[SimplePackedSerialization.h:201-232](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L201-L232) —— `SPSTuple<...>` 仅内嵌 `AsArgList`；`SPSString = SPSSequence<char>`；`SPSMap<K,V> = SPSSequence<SPSTuple<K,V>>`。注意它们在此处只是**前向声明**（`SPSOptional`/`SPSSequence`），具体特化在下方。

**「平凡序列」钩子**是让 `std::string` / `std::vector` / `std::unordered_map` 免写完整特化的糖：你只需特化 `TrivialSPSSequenceSerialization` / `TrivialSPSSequenceDeserialization`，提供 `reserve` / `append`，SPS 自带的序列 trait 就会帮你处理「uint64 长度 + 逐元素」。
[SimplePackedSerialization.h:244-349](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L244-L349) —— 为 `std::string`（视作 char 序列）、`std::vector<T>`、`span<T>`、`std::unordered_map` 各提供 `available = true` 的钩子，并实现 `reserve`/`append`。

被这些钩子启用的通用序列 trait：
[SimplePackedSerialization.h:351-390](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L351-L390) —— `size` 累加「uint64 长度 + 各元素」；`serialize` 先写长度再循环写元素；`deserialize` 先读长度、`reserve`、再循环 `append`。失败立即短路。

`SPSTuple` 对 `std::tuple` 与 `std::pair` 的特化，体现「标签只搭骨架、`SPSArgList` 干活」：
[SimplePackedSerialization.h:441-496](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L441-L496) —— 用 `std::index_sequence` 把 tuple 的每个字段展开成参数包，再整体交给 `AsArgList`。

`SPSOptional` 的 wire 格式是「1 字节 bool 哨兵 + 条件性的值」：
[SimplePackedSerialization.h:499-528](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L499-L528) —— 先写 `!!Value`（有无），仅当为真才写值；反序列化先读哨兵，按其决定是否构造值。

零拷贝反序列化的范例——`std::string_view` 直接指向输入缓冲：
[SimplePackedSerialization.h:530-560](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L530-L560) —— `deserialize` 用 `IB.data()` 记下起点、再 `IB.skip(Size)` 越过数据，把视图直接指向输入缓冲内部，**不复制任何字节**。`span<const char>` 的特化（4.1.3 提到的 420-438 行）同理。这正是 `SPSInputBuffer::data()/skip()` 存在的意义。

测试对各标签逐一验证往返，几个代表性用例：
[SimplePackedSerializationTest.cpp:111-114](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTest.cpp#L111-L114) —— `std::vector<int32_t>` 经 `SPSSequence<int32_t>` 往返。
[SimplePackedSerializationTest.cpp:197-206](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTest.cpp#L197-L206) —— `std::tuple` 与 `std::pair` 经 `SPSTuple<...>` 往返；注意 pair 用的是 `SPSTuple<int32_t, SPSString>` 标签。
[SimplePackedSerializationTest.cpp:208-216](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTest.cpp#L208-L216) —— `std::optional` 的「有值 / 空值」两种情形。
[SimplePackedSerializationTest.cpp:173-195](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTest.cpp#L173-L195) —— `span<const char>` 反序列化后**指向输入缓冲**（断言 `InS.data() == Buffer + 8`），验证零拷贝。

#### 4.3.4 代码实践

1. **目标**：用程序验证 4.3.2 里手算的 18 字节，确认「无填充」与「uint64 长度」编码。
2. **步骤**：写一段小程序（或新测试），用 `SPSArgList<int32_t, int32_t, SPSString>` 对值 `(10, -5, std::string("hi"))` 调 `size`，断言等于 18；再 `serialize` 进缓冲，把缓冲按字节 `printf` 成十六进制。
3. **现象**：打印应为 `0A 00 00 00 | FB FF FF FF | 02 00 00 00 00 00 00 00 | 68 69`，恰好与手算一致。
4. **预期结果**：`size` 断言通过，字节序列符合上式。
5. **待本地验证**：以本地编译运行结果为准。

#### 4.3.5 小练习与答案

**练习 1**：`SPSString` 既映射到 `std::string` 也映射到 `std::string_view`，但前者要复制、后者零拷贝。说出两者 `deserialize` 的实现差异。

**参考答案**：`std::string` 走「平凡序列」钩子，循环 `append` 把每个字符**复制**进字符串存储；`std::string_view` 的特化则用 `IB.data()` 取当前游标作起点、`IB.skip(Size)` 越过数据，让视图**直接指向输入缓冲内部**，不复制一字节。代价是 `string_view` 的生命周期受输入缓冲支配，缓冲释放后视图失效。

**练习 2**：`SPSOptional<int32_t>` 的空值（`std::nullopt`）在 wire 上占几个字节？有值时又占几个？

**参考答案**：空值只写 1 字节 bool 哨兵（false），占 **1 字节**；有值时写 1 字节 true + 4 字节 int32 = **5 字节**。

**练习 3**：为什么 `SPSMap<K,V>` 被定义成 `SPSSequence<SPSTuple<K,V>>`，而不是单独发明一种 map 格式？

**参考答案**：因为 map 在线上就是「若干 (key,value) 对的序列」，完全可以复用「序列 = 长度 + 逐元素」与「元组 = 逐字段」两条已有规则，无需新增格式。这是 SPS 用少量正交原语（原语 / 序列 / 元组 / 可选）组合出丰富类型的典型范例，也减少了「两端必须同步的 wire 规则」数量。

---

## 5. 综合实践

把三个模块串起来：**为自定义结构体特化 `SPSSerializationTraits`，并写一个往返单元测试。**

**任务**：定义一个结构体，含两个 `int32_t` 与一个 `std::string`；用标签 `SPSTuple<int32_t, int32_t, SPSString>` 让 SPS 懂得序列化它；再写测试验证往返正确。

**第 1 步：定义结构体与特化**（示例代码，非项目原有）：

```cpp
#include "orc-rt/SimplePackedSerialization.h"
#include <string>

struct MyRecord {
  int32_t Id;
  int32_t Score;
  std::string Name;
};

// 为 MyRecord 特化 SPS trait：线上格式 = SPSTuple<int32_t, int32_t, SPSString>。
// 三件套全部转交给 SPSArgList —— 这正是 4.2 讲的「组合优先」。
template <>
class orc_rt::SPSSerializationTraits<
    orc_rt::SPSTuple<int32_t, int32_t, orc_rt::SPSString>, MyRecord> {
  using AL = orc_rt::SPSArgList<int32_t, int32_t, orc_rt::SPSString>;

public:
  static size_t size(const MyRecord &R) { return AL::size(R.Id, R.Score, R.Name); }
  static bool serialize(orc_rt::SPSOutputBuffer &OB, const MyRecord &R) {
    return AL::serialize(OB, R.Id, R.Score, R.Name);
  }
  static bool deserialize(orc_rt::SPSInputBuffer &IB, MyRecord &R) {
    return AL::deserialize(IB, R.Id, R.Score, R.Name);
  }
};
```

注意：`std::string` 字段能直接用标签 `SPSString` 序列化，是因为 4.3.3 的「平凡序列」钩子已经为 `std::string` 开启了 `available = true`——你不必再为它写任何代码。这正是 SPS 可组合性的回报。

**第 2 步：写往返测试**。最省事的做法是直接复用测试工具 `blobSerializationRoundTrip`，但它默认用 `std::equal_to<MyRecord>`，而 `MyRecord` 没有内置 `==`，所以要传一个自定义比较器：

```cpp
#include "SimplePackedSerializationTestUtils.h"
#include "gtest/gtest.h"

TEST(MyRecordSerialization, RoundTrip) {
  auto Cmp = [](const MyRecord &A, const MyRecord &B) {
    return A.Id == B.Id && A.Score == B.Score && A.Name == B.Name;
  };
  blobSerializationRoundTrip<
      orc_rt::SPSTuple<int32_t, int32_t, orc_rt::SPSString>, MyRecord,
      /*Comparator=*/bool(*)(const MyRecord&, const MyRecord&)>(
      MyRecord{10, -5, "hi"}, Cmp);
}
```

> 若不想为比较器类型纠结，也可参照 `SPSOutputBuffer`/`SPSInputBuffer` 测试的写法，手动「`size`→分配→`serialize`→换 `SPSInputBuffer`→`deserialize`→逐字段 `EXPECT_EQ`」，逻辑与 `blobSerializationRoundTrip` 完全一致（见 [SimplePackedSerializationTestUtils.h:34-52](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SimplePackedSerializationTestUtils.h#L34-L52)）。

**第 3 步：构建并运行**（参见 u1-l2）。把新文件加入 `test/unit/CMakeLists.txt` 后执行 `check-orc-rt-unit` 目标。

**预期结果**：测试通过，且 `size({10, -5, "hi"})` 等于 18（与 4.3.2 手算一致）。**待本地验证**：实际数值以本地编译运行结果为准。

**延伸思考**：如果把 `std::string Name` 换成 `std::optional<int32_t>`，标签与特化要怎么改？（提示：字段标签改 `SPSOptional<int32_t>`，特化里 `AL` 多一个 `SPSOptional<int32_t>`，其余三件套写法不变。）

---

## 6. 本讲小结

- SPS 是 wrapper function **之上**的类型还原层：把任意 C++ 值打包成一串小端字节、再原样还原。通信层（u5-l1）只搬字节，SPS 才规定字节含义。
- 物理地基是两个零所有权游标：`SPSOutputBuffer`（带溢出检查的写游标）与 `SPSInputBuffer`（带下溢检查的读游标，`data()`/`skip()` 支持零拷贝视图）。
- 核心契约是 `SPSSerializationTraits<SPSTagT, ConcreteT>` 的三件套 `size / serialize / deserialize`，配合**两阶段**（先量后写、精确分配）。`SPSArgList` 用递归把多字段序列化粘合起来，是所有标签类型的公共引擎。
- 标签与具体类型**分离**：`SPSTagT` 描述线上 schema、`ConcreteT` 是内存类型，一个标签（如 `SPSString`）可映射多个宿主类型（`std::string` 复制、`std::string_view` 零拷贝）。
- wire 格式由少量正交原语组合而成：原语小端原样、`SPSSequence` = 长度 + 逐元素、`SPSTuple` = 逐字段无填充、`SPSOptional` = bool 哨兵 + 条件值；`SPSString`/`SPSMap` 都是前两者的别名。
- 「平凡序列」钩子让 `std::string` / `std::vector` / `std::unordered_map` 免写完整特化，自定义结构体也只需把三件套转交给 `SPSArgList`。

---

## 7. 下一步学习建议

- 下一讲 **u6-l2 SPSWrapperFunction**：把本讲的 SPS 接到 wrapper function 的 `call` / `handle` 上，得到**类型安全**的跨进程调用（如 `int32_t(int32_t, int32_t)` 的加法 wrapper），以及 `ORC_RT_SPS_WRAPPER` 宏。届时你会看到 SPS 如何把「裸字节」变成「带签名的函数调用」。
- 下一讲 **u6-l3 控制接口（sps-ci）**：把若干 SPS wrapper 处理器注册进符号表，让控制器能跨进程按名字调用它们。
- 进阶阅读：若想了解 SPS 如何序列化 orc-rt 自己的类型，可看 `SPSExecutorAddr`（地址）、`SPSError`/`SPSExpected`（错误与期望值）这几个特化，它们是把 u2-l3 的错误模型搬上 wire 的关键。
- 对照阅读：控制端对应实现位于 `llvm/ExecutionEngine/Orc/Shared/`（文件头注释指明）。两端 wire 格式是一份跨仓库契约，对照阅读能加深「为什么必须用最简、稳定的二进制格式」的理解。
