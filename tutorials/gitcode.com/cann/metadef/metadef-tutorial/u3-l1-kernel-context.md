# KernelContext 与 Chain：执行上下文的底层骨架

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚为什么 `gert::KernelContext` 和 `gert::Chain` 必须是 POD（standard layout），以及 `static_assert(std::is_standard_layout<...>)` 在守护什么。
2. 读懂 `Chain` 的「小对象内联 / 大对象指针」双模存储，以及 `AsyncAnyValue` 这个 C 结构体的内存布局。
3. 读懂 `KernelContext` 底层 `KernelRunContext` 的变长数组布局，独立追踪 `GetInput` / `GetOutput` / 属性访问三条取值链路。
4. 理解 `KernelContext`（骨架）与 `ExtendedKernelContext`（扩展层）的分工，为下一讲的 TilingContext、InferShapeContext 打底。

## 2. 前置知识

### 2.1 什么是 POD / standard layout

POD（Plain Old Data）指布局与 C 语言结构体兼容的数据类型。C++11 之后更精确的说法是 **standard layout**（标准布局），用 `std::is_standard_layout<T>::value` 判断。一个类是 standard layout，大致要求：

- 所有非静态成员访问权限一致（不能既有 `public` 又有 `private` 成员）；
- 没有虚函数、没有虚基类；
- 基类和成员也都要是 standard layout；
- 首个非静态成员与基类类型不同等布局约束。

满足这些约束后，这个类在内存里的排布就是可预测的：没有隐藏的虚表指针，没有编译器自由发挥的空间。这意味着：

1. **可以用 `memcpy` / `memset` 复制和清零**；
2. **可以在一块裸内存上 placement-new 直接构造**，不需要跑构造函数链；
3. **跨编译器、跨 so 的布局是一致的**——这正是 ABI 兼容的基石。

回顾 [u1-l1](u1-l1-project-overview.md) 讲过的背景：metadef 被 ge 和所有算子仓依赖，执行上下文由框架分配内存、由算子 so 中的函数读取，两边是**独立编译的代码**。如果上下文对象里出现 `std::string`、虚函数或智能指针，不同版本的编译器/标准库可能给出不同布局，算子 so 一读就越界。所以 metadef 的选择是：**执行上下文从内存布局上就是 C 兼容的**。

### 2.2 什么是「类型擦除」的值槽

上一单元的 [u2-l3](u2-l3-any-value-and-type-id.md) 讲过 `ge::AnyValue`：一个固定大小的容器，能装下任意类型的值。本讲的 `Chain` 是同一思想在执行期的极简版——一个 16 字节的槽（8 字节数据 + 8 字节删除器指针），小数据直接放槽里，大数据放指针。理解了 AnyValue，Chain 就是「去掉类型 ID、只留数据和删除器」的瘦身版。

### 2.3 变长数组技巧（柔性数组的变体）

C 语言里常见的「struct hack」：结构体最后一个成员声明为数组，实际分配内存时多分配一段，让数组「越界」使用。本讲的 `KernelRunContext::values[1]` 就是这个技巧——声明 1 个元素，实际可能装 N 个输入 + M 个输出。这是 C 侧定义、C++ 侧消费的经典跨界手法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/external/exe_graph/runtime/kernel_run_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_run_context.h) | 纯 C 头文件，定义 `AsyncAnyValue`（Chain 的底层）与 `KernelRunContext`（KernelContext 的底层），用 `extern "C"` 保证无名字修饰 |
| [inc/external/exe_graph/runtime/kernel_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h) | 本讲主角：`gert::Chain` 与 `gert::KernelContext` 的 C++ 壳，全部 inline 实现 |
| [inc/external/exe_graph/runtime/extended_kernel_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h) | `ExtendedKernelContext`：在 KernelContext 之上提供 TensorDesc / 属性 / 节点信息等类型化访问，是下一批上下文类的公共基类 |
| [tests/ut/base/testcase/kernel_context_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/kernel_context_unittest.cc) | 单元测试，用 `reinterpret_cast` 直接在裸 `AsyncAnyValue` 上操作 Chain，验证内存布局假设 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. `AsyncAnyValue` 与 `KernelRunContext`：C 层的内存契约
2. `Chain`：16 字节类型擦除值槽
3. `KernelContext`：input/output 的随机访问定位
4. 属性从哪来：`KernelContext` 与 `ExtendedKernelContext` 的分工

### 4.1 AsyncAnyValue 与 KernelRunContext：C 层的内存契约

#### 4.1.1 概念说明

metadef 把执行上下文最底层的两个结构体放在一个**纯 C 头文件**里（`extern "C"` 包裹，无任何 C++ 特性）。这是刻意为之：

- C 结构体没有名字修饰（name mangling），布局由 ABI 固定，任何语言、任何编译器版本看到的内存都一样；
- 后续 C++ 壳类（`Chain`、`KernelContext`）的成员就是这些 C 结构体本身，保证壳类也是 standard layout。

#### 4.1.2 核心流程

`AsyncAnyValue` 的 16 字节布局：

```
偏移 0 ┌────────────────────────────┐
       │ union {                    │
       │   void *pointer;           │  ← 大对象：指向堆上数据的指针
       │   unsigned char            │
       │     inplace[sizeof(void*)];│  ← 小对象：≤8 字节数据就地存放
       │ } data                     │
偏移 8 ├────────────────────────────┤
       │ FreeCallback deleter       │  ← 释放回调，nullptr 表示无需释放
偏移16 └────────────────────────────┘
```

注意 `data` 是个联合体：`pointer` 和 `inplace` 共用同一块 8 字节。放小数据时这 8 字节本身存值；放大数据时这 8 字节存指针。**没有类型标记**——Chain 靠模板参数 `sizeof(T)` 在编译期决定按哪种方式解读，这就是它和 `ge::AnyValue`（带 TypeId）的核心区别：更快，但类型安全责任在使用者。

`KernelRunContext` 的布局：

```
偏移 0  size_t input_size            ← 输入个数
偏移 8  size_t output_size           ← 输出个数
偏移16  const void *compute_node_info   ← 指向 ComputeNodeInfo（节点信息/属性）
偏移24  const void *kernel_extend_info  ← 指向 KernelExtendInfo（kernel 扩展信息）
偏移32  AsyncAnyValue **output_start    ← 输出区起始指针（标记 todo delete this，历史遗留）
偏移40  AsyncAnyValue *values[1]       ← 柔性数组！实际长度 = input_size + output_size
```

`values` 数组里每个元素指向一个 `AsyncAnyValue`（即一个 Chain 槽）：前 `input_size` 个是输入，紧接着 `output_size` 个是输出。整个结构体按实际输入输出数量一次性分配，`values[1]` 只是占位声明。

#### 4.1.3 源码精读

C 层结构体定义在 kernel_run_context.h，两个结构体都有注释明确警告「不要直接引用和操作此数据结构」：

- [kernel_run_context.h:L25-L31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_run_context.h#L25-L31) —— `AsyncAnyValue`：union 数据槽 + 删除器回调，这是 Chain 的底层存储。
- [kernel_run_context.h:L20-L20](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_run_context.h#L20-L20) —— `typedef void (*FreeCallback)(void *)`：释放回调的统一签名。
- [kernel_run_context.h:L36-L43](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_run_context.h#L36-L43) —— `KernelRunContext`：两个 size 计数、两个扩展信息指针、柔性数组 `values[1]`。注意 `output_start` 旁的 `// todo delete this` 注释，说明它是待清理的历史兼容字段。

#### 4.1.4 代码实践

**实践目标**：用 `offsetof` / `sizeof` 验证上面的布局推断。

**操作步骤**（示例代码，可作为独立的小程序编译运行，或挂进单测）：

```cpp
// 示例代码：验证 C 层结构体布局
#include <cstddef>
#include <cstdio>
#include "exe_graph/runtime/kernel_run_context.h"

int main() {
  printf("sizeof(AsyncAnyValue)   = %zu\n", sizeof(gert::AsyncAnyValue));
  printf("sizeof(KernelRunContext header) = %zu\n",
         offsetof(gert::KernelRunContext, values));
  printf("sizeof(void*) = %zu\n", sizeof(void *));
  return 0;
}
```

**需要观察的现象**：`sizeof(AsyncAnyValue)` 应为 16（64 位平台）；`values` 的偏移应为 40（5 个指针宽度的头部）。

**预期结果**：输出 `16`、`40`、`8`。若不符，说明平台指针宽度不同（如 32 位平台），布局假设需要重算。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AsyncAnyValue` 的 `data` 用 union 而不是直接声明 `void *pointer` 加一个 `unsigned char inplace[8]` 两个成员？

**答案**：union 让两种解读方式**共用同一块内存**，保证结构体大小固定为 16 字节；若声明成两个独立成员，大小变成 24 字节，且读写 `inplace` 不会反映到 `pointer`，联合体语义正是「同一份数据的多种视角」。

**练习 2**：`KernelRunContext` 里的 `values[1]` 为什么声明为长度 1 而不是 0 或不写长度？

**答案**：这是 C 结构体柔性数组的历史写法。C 标准不允许长度 0 的数组（部分编译器扩展支持），也不允许不写长度（那是 C++ 的另一种语法）。长度 1 在所有 C 编译器上都能编译，实际分配时按 `input_size + output_size` 个元素分配即可。C99 之后也可以用 `values[]`，但为了兼容老编译器，metadef 保留了 `[1]` 写法。

### 4.2 Chain：16 字节类型擦除值槽

#### 4.2.1 概念说明

`Chain` 是执行上下文里所有「值」的统一载体：一个输入张量的描述、一个属性值、一个整型参数，最终都以 Chain 槽的形式挂在 `KernelRunContext::values` 数组上。它解决的问题是：**框架侧往槽里放什么类型的数据，算子侧都能用同一个接口按指定类型取出来**——不经过任何虚函数或运行期类型查询（对比 `ge::AnyValue` 需要 TypeId 对质），代价是调用双方必须对「槽里是什么类型」有一致的约定。

它为什么叫 Chain（链）？因为框架构建时会把这些槽按 input→output 顺序链在 context 后面，`KernelContext` 沿槽序列做随机访问（见 4.3 节）。

#### 4.2.2 核心流程

读取一个值的判定流程：

```
Chain::GetValue<T>() / GetPointer<T>()
        │
        ├── sizeof(T) <= sizeof(void*)  →  按 inplace 解读：数据就在本槽的 data 字节里
        │                                    （编译期 enable_if 选中第一个重载）
        └── sizeof(T) >  sizeof(void*)  →  按 pointer 解读：data.pointer 指向堆上的 T
                                             （编译期 enable_if 选中第二个重载）
```

写入一个堆对象：

```
SetWithDefaultDeleter(ptr)
        │
        ├── Set(ptr, reinterpret_cast<FreeCallback>(DefaultDeleter<T>))
        │        │
        │        ├── FreeResource()          ← 若槽里已有带 deleter 的旧数据，先释放
        │        ├── data.pointer = ptr
        │        └── deleter = 传入的回调
```

关键点：**选择哪个解读方式的依据是编译期 `sizeof(T)`**，两个 `GetPointer` 重载靠 `std::enable_if<(sizeof(T) <= sizeof(void *)), int>::type = 0` 这样的模板 SFINAE 互斥分流。这也是为什么把 `TestT{int64_t a; int32_t b;}`（12 字节，会被 padding 到 16）当作大对象用指针解读。

#### 4.2.3 源码精读

Chain 全部实现都在头文件里，长约 110 行：

- [kernel_context.h:L16-L18](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L16-L18) —— `class Chain` 与 `Deleter` 类型别名（`void (*)(void *)`，与 `FreeCallback` 同构）。
- [kernel_context.h:L24-L27](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L24-L27) —— 小对象版 `GetPointer`：`enable_if<sizeof(T) <= sizeof(void*)>` 命中时，把 `data.inplace` 字节重解释为 `T` 的地址。
- [kernel_context.h:L33-L36](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L33-L36) —— 大对象版 `GetPointer`：返回 `data.pointer` 所指的 `T`。注意两个重载签名完全相同，仅 enable_if 条件相反，靠 SFINAE 二选一。
- [kernel_context.h:L60-L72](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L60-L72) —— `GetValue` 只对**小对象**提供（返回 `T&` 可写）；大对象请走 `GetPointer` 解引用。注意非 const 版本可原地改写槽内数据。
- [kernel_context.h:L78-L82](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L78-L82) —— `Set`：先 `FreeResource()` 释放旧值，再存指针与 deleter。`deleter == nullptr` 表示数据无需释放（例如指向 context 自身或其他生命周期由别人管理的内存）。
- [kernel_context.h:L88-L101](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L88-L101) —— `SetWithDefaultDeleter` 两个重载：非数组类型用 `delete`，数组类型用 `delete[]`，并都 `reinterpret_cast` 成统一的 `FreeCallback` 签名。
- [kernel_context.h:L121-L127](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L121-L127) —— 私有工具：`FreeResource` 只在 deleter 非空时回调；唯一成员变量是 `AsyncAnyValue any_value_`，即 4.1 节的 C 结构体。
- [kernel_context.h:L129-L129](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L129-L129) —— `static_assert(std::is_standard_layout<Chain>::value, ...)`：编译期守护，任何破坏布局的改动（比如加虚函数、混用访问权限的成员）直接编译失败。

单元测试直接验证了布局假设——它根本不通过正常构造，而是把裸 `AsyncAnyValue` 重解释成 `Chain` 来用：

- [kernel_context_unittest.cc:L22-L30](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/kernel_context_unittest.cc#L22-L30) —— `ChainGetInnerOk`：对默认构造的 Chain 调 `GetPointer<uint64_t>`，断言返回值等于 `&c` 本身——证明小对象就存在 Chain 自己的地址处。
- [kernel_context_unittest.cc:L32-L42](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/kernel_context_unittest.cc#L32-L42) —— `ChainGetAllocOk`：`TestT`（>8 字节）走 pointer 分支，返回 `av.data.pointer`。
- [kernel_context_unittest.cc:L43-L55](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/kernel_context_unittest.cc#L43-L55) —— `ChainGetInnerValueOk`：通过 `GetValue<int64_t>() = 20` 原地改写，再断言 `av.data.pointer` 变成 20——同一块内存的两种视角互相印证。
- [kernel_context_unittest.cc:L57-L71](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/kernel_context_unittest.cc#L57-L71) —— `ChainSetDeleterOk`：`SetWithDefaultDeleter(new TestT())` 后 `av.deleter` 非空，手动调 `av.deleter(av.data.pointer)` 释放，验证 deleter 确实是可调用的 `delete` 封装。

#### 4.2.4 代码实践

**实践目标**：亲手复现「同一块 16 字节、两种解读」的机制。

**操作步骤**：

1. 阅读上面的 [kernel_context.h:L24-L36](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L24-L36)，确认两个 `GetPointer` 重载的唯一区别是 `enable_if` 条件。
2. 参考 [kernel_context_unittest.cc:L43-L55](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/kernel_context_unittest.cc#L43-L55) 的写法，写出自己的理解版（示例代码）：

```cpp
// 示例代码：验证小对象内联存储
gert::AsyncAnyValue av = {nullptr, nullptr};
av.data.pointer = reinterpret_cast<void *>(0x1234);   // 往 8 字节槽里放一个"值"
gert::Chain *c = reinterpret_cast<gert::Chain *>(&av); // 裸内存重解释成 Chain
int64_t v = c->GetValue<int64_t>();                    // 小对象：按 inplace 解读
// 预期 v == 0x1234，因为两种视角读的是同一块字节
```

3. 把它改写成一个 gtest 用例，放进 `tests/ut/base/testcase/` 下新文件（参考 [u1-l2](u1-l2-build-and-test.md)：`ut_metadef` 用 glob 收集 `tests/ut/base/testcase/*.cc`，新增文件无需改 CMake）。
4. 运行 `bash tests/run_test.sh -u`，用 `--gtest_filter` 只跑你的用例。

**需要观察的现象**：`GetValue<int64_t>()` 返回值与写入 `data.pointer` 的整数一致；若换成 `struct {int64_t a; int32_t b;}`（>8 字节）则 `GetPointer` 返回的是 `data.pointer` 存的地址本身。

**预期结果**：断言通过，证明小对象内联、大对象走指针的分流完全由 `sizeof(T)` 决定。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果调用方对同一个槽先 `SetWithDefaultDeleter(new std::vector<int64_t>())`，再 `SetWithDefaultDeleter(new std::vector<int64_t>())`，第一个 vector 会泄漏吗？

**答案**：不会。`Set` 的第一步就是 `FreeResource()`（[kernel_context.h:L79](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L79)），deleter 非空时先对旧数据调用 `DefaultDeleter<std::vector<int64_t>>`（即 `delete`）再存新值。

**练习 2**：为什么 `Set` 的 deleter 参数允许传 `nullptr`？

**答案**：nullptr 表示该槽对数据不持有所有权。典型场景：数据指向 context 自身缓冲、静态常量区或生命周期由框架其他机制管理的内存。此时 Chain 只是「带视图的窗口」，释放责任在所有者手里。这与 [u2-l4](u2-l4-shape-stride-tensor.md) 里 `TensorData` 的 `kFollowing` 布局思路一脉相承——不是所有指针都需要释放。

**练习 3**：`GetValue` 为什么不给大对象（`sizeof(T) > 8`）提供重载？

**答案**：`GetValue` 返回 `T&` 且直接从 `inplace` 字节重解释。对大对象这毫无意义——数据在堆上，正确做法是 `*GetPointer<T>()`。省掉这对重载可以避免使用者误把 `data.pointer` 的 8 字节指针值当成 T 本身解读。

### 4.3 KernelContext：input/output 的随机访问定位

#### 4.3.1 概念说明

`KernelContext` 是算子函数拿到的第一个参数的类型根基。它唯一的成员变量就是一个 `KernelRunContext context_`，自身只加了一层 inline 的便捷方法：数量查询、按下标取 Chain、类型化取值、以及两个扩展信息指针的透出。

设计要点：**输入和输出共用同一个 `values` 数组，输出紧跟在输入后面**。所以「随机访问定位」的公式是：

\[ \text{values 索引} = \begin{cases} i & \text{第 } i \text{ 个输入} \\ \text{input\_size} + i & \text{第 } i \text{ 个输出} \end{cases} \]

#### 4.3.2 核心流程

三条典型取值链路：

```
① GetInputPointer<T>(i)
   GetInput(i)                              ← 越界返回 nullptr
      └── reinterpret_cast<Chain*>(context_.values[i])
   └── chain->GetPointer<T>()               ← 按 sizeof(T) 选 inplace/pointer 分支

② GetOutputPointer<T>(i)
   GetOutput(i)                             ← 越界返回 nullptr
      └── reinterpret_cast<Chain*>(context_.values[context_.input_size + i])
   └── chain->GetPointer<T>()

③ 属性（注意：不在 values 数组里！）
   GetComputeNodeExtend()                   ← 返回 context_.compute_node_info
      └── ExtendedKernelContext::GetAttrs() ← 从 ComputeNodeInfo 中取 RuntimeAttrs
```

第三条链路是本讲最容易被误解的地方：**`KernelContext` 本身没有 `GetAttr` 接口，属性不在 values 槽序列中**，而是挂在 `compute_node_info` 指针指向的 `ComputeNodeInfo` 结构上，由派生类 `ExtendedKernelContext` 提供类型化访问（见 4.4 节）。输入输出是「逐槽并列」的数据，属性是「成包存放」的字典——两种组织方式服务于不同的访问模式。

#### 4.3.3 源码精读

- [kernel_context.h:L137-L146](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L137-L146) —— `GetInputNum`/`GetOutputNum`：直接读 `input_size`/`output_size`。
- [kernel_context.h:L152-L157](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L152-L157) —— `GetInput`：越界保护后把 `values[i]` 重解释为 `const Chain *`。
- [kernel_context.h:L174-L190](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L174-L190) —— `GetOutput` 两个版本：索引公式 `values[input_size + i]`，mutable 版返回可写 `Chain *`。
- [kernel_context.h:L191-L196](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L191-L196) —— `GetOutput2`：走遗留的 `output_start` 指针定位，与 `GetOutput` 等价但路径不同，属过渡期兼容。
- [kernel_context.h:L203-L210](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L203-L210) —— `GetInputValue<T>`：`GetInput` + `GetValue` 的组合糖；Chain 不存在时返回 `T{}`（值初始化零值），是「失败不崩溃」风格的体现。
- [kernel_context.h:L246-L252](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L246-L252) —— `GetInputStrPointer`：字符串输入特化，`GetValue<const char*>` 直接把指针值取回。源码注释里留着 `todo 特化一个模板就可以了`，是可改进点。
- [kernel_context.h:L257-L266](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L257-L266) —— `GetComputeNodeExtend`/`GetKernelExtend`：把两个 `const void*` 透出，类型化由上层完成（KernelContext 有意在骨架层保持「无类型」）。
- [kernel_context.h:L299-L316](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L299-L316) —— `GetContext`（注释警告非框架代码勿用）与 `IsInlineSize`（`size <= sizeof(void*)` 判定，把内联阈值显式暴露给框架构建侧）。
- [kernel_context.h:L318-L321](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L318-L321) —— 唯一成员 `KernelRunContext context_` 与第二处 `static_assert(std::is_standard_layout<KernelContext>::value, ...)`。

#### 4.3.4 代码实践

**实践目标**：亲手在裸内存上拼一个最小 KernelRunContext，跑通 GetInput/GetOutput 链路（这也是框架 ContextBuilder 的核心动作，为 [u5-l1](u5-l1-context-builder.md) 埋伏笔）。

**操作步骤**（示例代码）：

```cpp
// 示例代码：手工组装 2 输入 1 输出的 KernelContext
#include "exe_graph/runtime/kernel_context.h"
using namespace gert;

const size_t kInNum = 2;
const size_t kOutNum = 1;
// 一次性分配：头部 + (输入+输出) 个 Chain 槽
auto *ctx = reinterpret_cast<KernelContext *>(
    new unsigned char[sizeof(KernelContext) + (kInNum + kOutNum) * sizeof(Chain)]{});

KernelRunContext *raw = ctx->GetContext();
raw->input_size = kInNum;
raw->output_size = kOutNum;
auto *slots = reinterpret_cast<Chain *>(raw->values);
for (size_t i = 0; i < kInNum + kOutNum; ++i) {
  raw->values[i] = &slots[i];   // values[i] 指向紧跟其后的第 i 个槽
}
slots[0].GetValue<int64_t>() = 42;   // 输入 0：小对象内联写入

// 验证：GetInputPointer 应取回 42，GetOutput(0) 应与 slots[2] 同址
// EXPECT_EQ(*ctx->GetInputPointer<int64_t>(0), 42);
// EXPECT_EQ(ctx->GetOutput(0), &slots[2]);
delete[] reinterpret_cast<unsigned char *>(ctx);
```

1. 把上面代码改写为 gtest 用例（断言见注释）。
2. `bash tests/run_test.sh -u -- --gtest_filter=<你的用例名>` 运行。
3. 试着把 `raw->values[i] = &slots[i]` 改成指向错误下标，观察断言如何失败——这能帮你确认索引公式。

**需要观察的现象**：`GetInputPointer<int64_t>(0)` 返回 42；`GetOutput(0)` 的地址恰好是第 3 个槽；越界调用 `GetInputPointer<int64_t>(99)` 返回 `nullptr` 而不崩溃。

**预期结果**：三条断言全部通过。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`GetOutput(i)` 为什么能用 `values[input_size + i]` 而不需要单独的输出数组？

**答案**：因为框架构建 context 时就把输出槽紧挨着输入槽放在同一个 values 数组里，布局契约是「输入在前、输出在后」。`input_size` 就是输出区起点的偏移。这样一次分配、一个数组即可，且输出同样能靠下标随机访问。

**练习 2**：`GetInputValue<T>(i)` 在输入不存在时返回什么？为什么这样设计而不是抛异常？

**答案**：返回 `T{}`（值初始化，如整数得 0、指针得 nullptr）。执行上下文运行在算子 so 里，跨 ABI 边界抛异常不可靠（不同 so 可能链接不同异常运行时），metadef 的惯例是「失败返回空值/错误码，绝不崩溃」——与 [u2-l2](u2-l2-ascend-string.md) 里 AscendString 的 nothrow 风格一致。

**练习 3**：`MutableInput` 和 `GetInput` 都声明为 `const` 成员函数，返回可写指针却合法，这说明什么？

**答案**：`MutableInput` 返回的 `Chain *` 指向 `values[i]` 所指的槽内存，而槽本身不在 `KernelContext` 对象内部（context 只存指针）。修改槽内容不改变对象自身的成员，所以 const 语义上不冲突——这是「指针间接层」绕过 const 的典型场景，也提醒我们 KernelContext 的 const 只保护头部字段，不保护槽数据。

### 4.4 属性从哪来：KernelContext 与 ExtendedKernelContext 的分工

#### 4.4.1 概念说明

`KernelContext` 故意只做「骨架」：它知道有多少输入输出、槽在哪、扩展信息指针在哪，但对槽里装的具体类型（TensorDesc？Shape？int64？）一无所知，也**完全没有属性接口**。类型化访问全部交给 `ExtendedKernelContext`：

- 它以 `protected` 方式继承 `KernelContext`——注意是 protected 继承加 `reinterpret_cast`/`static_cast` 的组合拳，不添加任何数据成员（否则布局就变了）；
- 它把 `compute_node_info` 静态转换成 `ComputeNodeInfo *`，由此提供 `GetAttrs()`（返回 `RuntimeAttrs`）、`GetInputDesc`、`GetNodeType` 等类型化接口；
- 下一讲的 TilingContext、InferShapeContext 等都继承自它。

这就是本讲标题里「三类句柄」的真实答案：**input/output 句柄 = values 数组随机访问；attr 句柄 = compute_node_info 指针间接访问**。两条通路、两种组织方式。

#### 4.4.2 核心流程

```
算子侧调用 GetAttrs()
   │
   ├── ExtendedKernelContext::GetComputeNodeInfo()
   │      └── static_cast<const ComputeNodeInfo*>(KernelContext::GetComputeNodeExtend())
   │             └── 返回 context_.compute_node_info
   └── compute_node_info->GetAttrs() → const RuntimeAttrs*
```

动态输入（DYNAMIC_INPUT）的定位则把两条通路串了起来：

```
GetDynamicInputDesc(ir_index, relative_index)
   ├── GetIrInputInstanceInfo(ir_index)          ← 从 ComputeNodeInfo 查实例化信息
   │      └── ins_info->GetInstanceStart()       ← 该 IR 输入实例化后的起始槽位
   └── GetInputTdInfo(ins_info->GetInstanceStart() + relative_index)
```

即：先查「属性侧」的实例化信息拿到起始下标，再回到「槽序列侧」做随机访问。

#### 4.4.3 源码精读

- [extended_kernel_context.h:L18-L18](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L18-L18) —— `class ExtendedKernelContext : protected KernelContext`：protected 继承，外部不能把它当 KernelContext 用，只暴露自己的类型化接口。
- [extended_kernel_context.h:L133-L139](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L133-L139) —— `GetAttrs`：属性访问的入口，从 `ComputeNodeInfo` 取 `RuntimeAttrs`；注释说明只有 IR 原型中定义过的属性才能取到。
- [extended_kernel_context.h:L166-L168](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L166-L168) —— `GetComputeNodeInfo`：把骨架层的 `const void*` 静态转换为 `const ComputeNodeInfo *`，这是「无类型指针 → 类型化视图」的关键一跳。
- [extended_kernel_context.h:L55-L68](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L55-L68) —— `GetDynamicInputDesc`：先查实例化信息（含 `GetInstanceNum` 越界检查），再用 `GetInstanceStart() + relative_index` 回到 values 槽序列——两条通路的交汇点。
- [extended_kernel_context.h:L199-L223](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L199-L223) —— protected 的 `GetDynamicInputPointer`/`GetDynamicOutputPointer`：给派生上下文（TilingContext 等）复用的模板方法，输出定位公式 `input_num + instance_start + relative_index` 正是 4.3 节索引公式的动态版。
- [extended_kernel_context.h:L225-L226](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L225-L226) —— 第三处 `static_assert(std::is_standard_layout<ExtendedKernelContext>::value, ...)`：整条继承链全程被 POD 约束守护。

#### 4.4.4 代码实践

**实践目标**：数一数 metadef 用 `static_assert(std::is_standard_layout<...>)` 守护了多少个上下文类。

**操作步骤**：

1. 在仓库根目录执行搜索（只读操作）：`grep -rn "is_standard_layout" inc/external/exe_graph/`。
2. 对每个命中的类，记录：类名、所在文件、断言行号。
3. 挑其中一个（如 `TilingContext`，将在 [u3-l3](u3-l3-tiling-context.md) 精讲），检查它是否新增了数据成员——你会发现答案是没有，它只加方法。

**需要观察的现象**：exe_graph/runtime 下所有上下文类（Chain、KernelContext、ExtendedKernelContext 及各具体上下文）都带这条断言；它们全部不含新增非静态数据成员。

**预期结果**：所有上下文类共享同一份由 `KernelRunContext` 定义的内存布局，算子 so 拿到的任何上下文指针都可安全重解释——这正是 `static_assert` 存在的意义：**把「布局契约」从文档约定升级为编译期硬约束**，谁不小心加了一个 `std::string` 成员或虚函数，构建直接失败，而不是在客户现场出现诡异的内存越界。

#### 4.4.5 小练习与答案

**练习 1**：`ExtendedKernelContext` 为什么用 protected 继承而不是 public 继承？

**答案**：public 继承会让外部代码把 `ExtendedKernelContext*` 转成 `KernelContext*` 并直接调用骨架层接口，绕过类型化封装破坏分层。protected 继承切断了这条向上转换的通道，使用者只能看到扩展层精心设计的接口（GetInputDesc、GetAttrs 等），骨架层方法变成实现细节。

**练习 2**：既然 `ExtendedKernelContext` 一个数据成员都没加，那 `TilingContext`（继承它）凭什么能提供比 KernelContext 更多的信息？

**答案**：信息全部来自**context 头部两个指针所指向的外部结构**（`compute_node_info` → ComputeNodeInfo，`kernel_extend_info` → KernelExtendInfo）以及 values 槽里放置的不同类型数据。派生上下文类只是「对同一块内存的不同解读视图」加便捷方法，不改变布局——这就是这套体系能用 `reinterpret_cast` 在裸内存上构造任何上下文的全部秘密。

**练习 3**：如果给 `KernelContext` 增加一个虚函数，会发生什么？

**答案**：编译失败。`std::is_standard_layout` 要求无虚函数，[kernel_context.h:L321](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L321) 的 `static_assert` 会在编译期拦截。即使没有这条断言，虚函数也会引入虚表指针改变对象大小，导致「sizeof(KernelContext) + N*sizeof(Chain)」的内存分配公式失配、槽区错位——跨 so 读取时数据全部错乱。

## 5. 综合实践

**任务：把三条取值链路画成一张完整的内存布局图，并用单测验证。**

1. **画图**：拿出一张纸（或 mermaid），画出 64 位平台上一段连续内存：`[KernelRunContext 头部 40 字节][Chain 槽 0][槽 1]...[槽 N]`，标注：
   - 头部各字段偏移（input_size/output_size/compute_node_info/kernel_extend_info/output_start/values）；
   - 每个槽内部 16 字节的 union + deleter 结构；
   - 三条箭头链：`GetInputPointer<T>(i)`、`GetOutputPointer<T>(i)`、`GetAttrs()` 各自经过的字段与指针跳转。
2. **验证**：把 4.3.4 的手工组装代码扩展为一个 gtest 文件，新增用例：2 输入 1 输出，输入 0 内联写 `int64_t`，输入 1 用 `SetWithDefaultDeleter(new std::vector<int64_t>({1,2,3}))`，输出 0 留空。分别用 `GetInputValue`、`GetInputPointer<std::vector<int64_t>>`、`GetOutput(0)` 验证取值正确、地址符合你的图。
3. **解释**：在图旁用三五行文字回答——`static_assert(std::is_standard_layout<...>)` 在 kernel_context.h 中出现了两处（Chain 与 KernelContext），它们分别在守护什么？（提示：一个守护槽本身的 16 字节布局，一个守护「头部 + 变长槽」的整体可构造性。）
4. **运行**：`bash tests/run_test.sh -u -- --gtest_filter=你的用例前缀.*`，确认全部通过。若无 Ascend 环境，`run_test.sh` 的 UT 模式依赖 stub 机制可脱离硬件运行，但仍需先完成 [u1-l2](u1-l2-build-and-test.md) 的环境准备（`ASCEND_HOME_PATH` 等）；无法运行时标注「待本地验证」并保留代码。

## 6. 本讲小结

- 执行上下文的最底层是纯 C 结构体：16 字节的 `AsyncAnyValue`（union 数据槽 + deleter）与变长的 `KernelRunContext`（头部 + values 柔性数组），布局即 ABI 契约。
- `Chain` 是极简类型擦除槽：`sizeof(T) <= 8` 内联存储、否则存指针，分流由编译期 `enable_if` 完成，无运行期类型检查——快，但要求调用双方对槽内类型有共识。
- `KernelContext` 的随机访问公式：输入 `values[i]`、输出 `values[input_size + i]`；属性不在槽序列中，而是经 `compute_node_info` 指针从 `ComputeNodeInfo` 上取（由 `ExtendedKernelContext` 的 `GetAttrs` 提供）。
- 所有上下文类零新增数据成员，派生类只是同一块内存的类型化视图；三处 `static_assert(std::is_standard_layout<...>)` 把布局契约固化为编译期约束。
- 失败语义统一为「返回空值/nullptr，不崩溃不抛异常」，这是跨 ABI 边界代码的统一风格。

## 7. 下一步学习建议

下一讲 [u3-l2：ExtendedKernelContext 与 KernelRunContext：上下文的扩展层](u3-l2-extended-context.md) 将深入「框架如何在分配好的裸内存上构造具体上下文」的细节。之后按依赖顺序学习 [u3-l3：TilingContext](u3-l3-tiling-context.md) 与 [u3-l4：推理上下文](u3-l4-infer-contexts.md)，看真实的 Tiling/InferShape 算子函数如何消费本讲的骨架。建议同步精读 `inc/external/exe_graph/runtime/context_extend.h` 中 `ComputeNodeInfo`、`AnchorInstanceInfo` 的定义，理解属性包与实例化信息的完整结构。
