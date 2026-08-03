# 执行端地址：ExecutorAddr

## 1. 本讲目标

通过本讲你将：

- 理解为什么 orc-rt 要自己定义一个 `ExecutorAddr` 类型，而不直接用裸指针或 `uint64_t`。
- 掌握 `ExecutorAddr` 在「指针」与 `uint64_t` 之间的安全互转：`fromPtr` / `toPtr`。
- 学会用 `ExecutorAddrRange` 表示一段连续地址区间，并使用 `contains` / `overlaps` / `toSpan` 做区间判定。
- 了解 `Tag` / `Untag` 两个「指针包装函数」如何支持带标签指针（tagged pointer）场景。

本讲的所有结论都来自单一头文件 [include/orc-rt/ExecutorAddress.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h)，配套测试在 [test/unit/ExecutorAddressTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ExecutorAddressTest.cpp)。

## 2. 前置知识

在学习本讲前，你需要先建立「controller / executor 二分」的心智模型（见 [u2-l1](u2-l1-controller-executor-architecture.md)）。其中有两点和本讲直接相关：

1. **跨进程通信只搬字节**。controller 与 executor 可能不在同一个进程里。一个进程里的指针（如 `0x7ffe...`）在另一个进程里毫无意义。因此，凡是需要跨端传递的地址，都必须能被表示成一个「与平台指针等宽的整数」，再通过序列化层（后续 SPS 讲义）打包成字节流。
2. **executor 端要管理 JIT 代码占用的内存**。JIT 出来的代码、数据会被放进一段段内存区间。executor 需要用「地址区间」来记录哪段内存属于哪块代码、是否可执行、何时释放。

这两件事共同引出了本讲的两个核心类型：

| 类型 | 作用 | 类比 |
| --- | --- | --- |
| `ExecutorAddr` | 表示执行端「单个地址」 | 一个 `uint64_t`，但带类型安全 |
| `ExecutorAddrRange` | 表示执行端「一段地址区间」 | `[Start, End)` 半开区间 |

此外，你还需要一些 C++ 基础：模板、`reinterpret_cast`、CRTP（仅 `Tag`/`Untag` 用到一点函数对象思想，不深）。如果你对「带标签指针」完全陌生也不用担心——本讲会用一小节专门解释。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它是全库的基础设施，被几乎每一个 Service 和序列化代码使用。

| 文件 | 作用 |
| --- | --- |
| [include/orc-rt/ExecutorAddress.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h) | 定义 `ExecutorAddr`、`ExecutorAddrRange`、地址与偏移的加减运算、`Tag`/`Untag` 包装函数，以及 `std::hash` 特化。纯头文件，无 `.cpp`。 |
| [test/unit/ExecutorAddressTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ExecutorAddressTest.cpp) | 用 GoogleTest 覆盖默认值、排序、指针往返、函数指针、Tag/Untag、区间运算、哈希等。本讲的实践任务将大量参照这里的断言。 |

> 提示：orc-rt 里测试文件名和被测头文件名几乎一一对应（`ExecutorAddress.h` ↔ `ExecutorAddressTest.cpp`），这是反向定位源码的捷径（详见 [u1-l3](u1-l3-directory-layout.md)）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** `ExecutorAddr`：地址的「类型安全整数」，以及它与指针的互转。
- **4.2** `ExecutorAddrRange`：地址区间与 `contains` / `overlaps` / `toSpan`。
- **4.3** `Tag` / `Untag`：带标签指针的包装与解包装。

### 4.1 ExecutorAddr：地址与指针互转

#### 4.1.1 概念说明

先回答一个直觉问题：**为什么不用 `uint64_t`？为什么不用裸指针？**

- 直接用 `uint64_t`：可以在跨进程时序列化，但**丢失了类型信息**。`uint64_t` 可以和另一个 `uint64_t`（比如一个计数器）相加而不报错；地址加上另一个地址在语义上是错的（地址 + 地址 = 偏移？不，地址 + 偏移 = 地址）。
- 直接用裸指针 `T*`：保留了类型，但**无法跨进程序列化**，也无法统一表示「任意类型」的地址（你不能把 `void(*)()` 和 `int*` 放进同一个容器而不做类型擦除）。

`ExecutorAddr` 的设计目标就是**兼得两者好处**：内部用一个 `uint64_t` 存储（可序列化、宽度固定），但用一套精心设计的运算符和转换函数把「语义」钉死——

- 地址 + 偏移 = 地址
- 地址 − 地址 = 偏移
- 地址之间可比较大小、可判等
- 想要得到真正的指针，必须显式地、带目标类型地转换

这里有一个关键类型别名（[ExecutorAddress.h:26](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L26)）：

```cpp
using ExecutorAddrDiff = uint64_t;
```

`ExecutorAddrDiff` 表示「两个地址之差」，也就是偏移量。它在语义上和 `ExecutorAddr` 截然不同，但在物理上都是 64 位无符号整数。这种「同存储、不同类型名」的做法是 orc-rt 表达意图的主要手段。

#### 4.1.2 核心流程

`ExecutorAddr` 的核心是「存储」与「转换」两件事，可以画成下面这张图：

```
        指针世界                          整数/序列化世界
   ┌──────────────┐    fromPtr(Ptr, Unwrap)    ┌─────────────────┐
   │  T*  / void()│  ───────────────────────►  │   ExecutorAddr  │
   │  函数指针     │                             │   (uint64_t Addr)│
   │              │  ◄───────────────────────  │                  │
   └──────────────┘    toPtr<T>(Wrap)           └─────────────────┘
        ▲                                                 │
        │                                  getValue/setValue
        │                                                 ▼
        │                                          uint64_t（可直接序列化）
```

要点：

1. `fromPtr` 把一个指针（任意类型）**单向**压扁成一个 `ExecutorAddr`；可选地附带一个 `Unwrap` 函数，先把指针「解包装」再压扁。
2. `toPtr<T>` 把 `ExecutorAddr` 还原成类型为 `T` 的指针；可选地附带一个 `Wrap` 函数，在还原后「再包装」。
3. 默认情况下 `Unwrap` / `Wrap` 都是恒等函数（`rawPtr`），即什么也不做，直接按位取值。
4. `getValue()` 直接吐出底层的 `uint64_t`，用于跨进程序列化。

#### 4.1.3 源码精读

**类的核心存储与构造**（[ExecutorAddress.h:29-73](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L29-L73)）：

```cpp
class ExecutorAddr {
public:
  ...
  constexpr ExecutorAddr() noexcept = default;
  explicit constexpr ExecutorAddr(uint64_t Addr) noexcept : Addr(Addr) {}
  ...
private:
  uint64_t Addr = 0;
};
```

注意构造函数前面的 `explicit`：这会阻止 `uint64_t` 隐式转换成 `ExecutorAddr`，从而避免「把一个随便的数字当地址用」。这是类型安全的第一个护栏。

**fromPtr：指针 → 地址**（[ExecutorAddress.h:75-81](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L75-L81)）：

```cpp
template <typename T, typename UnwrapFn = defaultUnwrap<T>>
static constexpr ExecutorAddr fromPtr(T *Ptr,
                                      UnwrapFn &&Unwrap = UnwrapFn()) {
  return ExecutorAddr(
      static_cast<uint64_t>(reinterpret_cast<uintptr_t>(Unwrap(Ptr))));
}
```

这段代码做了三件事：

1. 模板参数 `T` 从实参指针自动推导，所以调用时写 `fromPtr(&x)` 就够了，不必写 `<int>`。
2. 先用 `Unwrap(Ptr)`（默认恒等）处理指针，再用 `reinterpret_cast<uintptr_t>` 把指针变成与指针等宽的无符号整数。
3. 再 `static_cast<uint64_t>` 收敛到 64 位，构造 `ExecutorAddr` 返回。

> 为什么两段转换？`uintptr_t` 是「与指针等宽的整数类型」（32 位平台上是 32 位，64 位平台上是 64 位），而 `uint64_t` 永远 64 位。先转到 `uintptr_t` 保证「一定能放下这个指针」，再拓宽到 `uint64_t` 保证跨平台存储格式一致。

**toPtr：地址 → 指针** 有两个重载。一个面向「对象指针类型」`T*`（[ExecutorAddress.h:83-90](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L83-L90)）：

```cpp
template <typename T, typename WrapFn = defaultWrap<std::remove_pointer_t<T>>>
constexpr std::enable_if_t<std::is_pointer<T>::value, T>
toPtr(WrapFn &&Wrap = WrapFn()) const {
  uintptr_t IntPtr = static_cast<uintptr_t>(Addr);
  assert(IntPtr == Addr && "ExecutorAddr value out of range for uintptr_t");
  return Wrap(reinterpret_cast<T>(IntPtr));
}
```

另一个面向「函数类型」`T`（注意是 `void()` 而不是 `void(*)()`，[ExecutorAddress.h:92-99](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L92-L99)）：

```cpp
template <typename T, typename WrapFn = defaultWrap<T>>
constexpr std::enable_if_t<std::is_function<T>::value, T *>
toPtr(WrapFn &&Wrap = WrapFn()) const {
  uintptr_t IntPtr = static_cast<uintptr_t>(Addr);
  assert(IntPtr == Addr && "ExecutorAddr value out of range for uintptr_t");
  return Wrap(reinterpret_cast<T *>(IntPtr));
}
```

两个重载用 `std::enable_if_t` 做 SFINAE 分流：你传 `int*`（是指针类型）走第一个，你传 `void()`（是函数类型）走第二个。这样无论你习惯写函数指针 `void(*)()` 还是函数类型 `void()`，都能正确还原。

注意那行 `assert(IntPtr == Addr && ...)`：如果在 32 位平台上某个 `ExecutorAddr` 的值超过了 `uintptr_t` 能表示的范围（>4GB），还原就会损失信息，这里用断言在调试期把它拦下来——这是类型安全的第二个护栏。

**比较与自增运算符**（[ExecutorAddress.h:107-160](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L107-L160)）：定义了 `==`、`!=`、`<`、`<=`、`>`、`>=`，以及 `++`/`--`/`+=`/`-=`。这些都直接作用在内部 `Addr` 上，让 `ExecutorAddr` 用起来像一个「带类型的整数」。

**地址与偏移的加减** 用自由函数实现，把语义钉死（[ExecutorAddress.h:166-182](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L166-L182)）：

```cpp
// 地址 - 地址 = 偏移
inline constexpr ExecutorAddrDiff operator-(const ExecutorAddr &LHS,
                                            const ExecutorAddr &RHS) noexcept {
  return ExecutorAddrDiff(LHS.getValue() - RHS.getValue());
}
// 地址 + 偏移 = 地址
inline constexpr ExecutorAddr operator+(const ExecutorAddr &LHS,
                                        const ExecutorAddrDiff &RHS) noexcept {
  return ExecutorAddr(LHS.getValue() + RHS);
}
```

注意只定义了「地址 ± 偏移」和「地址 − 地址」，**没有**定义「地址 + 地址」。这就从编译期杜绝了「两个地址相加」这种语义错误——这正是自定义类型相对裸 `uint64_t` 的价值。

> 小坑提醒：[ExecutorAddress.h:148-150](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L148-L150) 的后置 `operator--(int)` 实现里写的是 `ExecutorAddr(Addr++)`（看起来像笔误，应为 `Addr--`）。实践中我们一般用 `-= Size` 或 `Start + Size`，不会用到后置 `--`，所以这里了解即可，不必依赖它的具体行为。这是阅读真实源码时常遇到的「历史遗留」细节。

#### 4.1.4 代码实践

我们来写一个最小的指针往返小程序，并真正调用还原出来的函数指针。

**实践目标**：验证 `fromPtr` / `toPtr` 能无损地把函数指针打包成 `ExecutorAddr` 再还原回来，并且还原后的指针可正常调用。

**操作步骤**：

1. 在你本地的 orc-rt 构建树（或任意能 `#include "orc-rt/ExecutorAddress.h"` 的工程）里新建一个 `.cpp`，内容如下（**示例代码**，非项目原有文件）：

   ```cpp
   // 示例代码：演示 ExecutorAddr 的指针往返
   #include "orc-rt/ExecutorAddress.h"
   #include <cstdio>

   using namespace orc_rt;

   static int add(int a, int b) { return a + b; }

   int main() {
     // 1) 把函数指针打包成 ExecutorAddr
     ExecutorAddr EA = ExecutorAddr::fromPtr(add);
     std::printf("addr value = 0x%llx\n",
                 static_cast<unsigned long long>(EA.getValue()));

     // 2) 还原成「函数类型」指针并调用（走 is_function 重载）
     auto *FPtr = EA.toPtr<int(int, int)>();
     std::printf("add(3, 4) = %d\n", FPtr(3, 4));

     // 3) 也可以用普通变量指针验证（走 is_pointer 重载）
     int X = 42;
     ExecutorAddr XAddr = ExecutorAddr::fromPtr(&X);
     int *XPtr = XAddr.toPtr<int *>();
     std::printf("X = %d\n", *XPtr);
     return 0;
   }
   ```

2. 编译并运行（需要 orc-rt 头文件可见，可参考 [u1-l2](u1-l2-build-and-config.md) 的构建方式；这里只演示用法，单独编译头文件即可）。

**需要观察的现象**：

- 打印出的 `addr value` 是一个非零的十六进制数（函数 `add` 的运行时地址）。
- `add(3, 4)` 输出 `7`，证明还原出的函数指针可被正常调用。
- `X` 输出 `42`，证明对象指针的往返同样无损。

**预期结果**：与上述一致。这等价于测试 [ExecutorAddressTest.cpp:42-60](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ExecutorAddressTest.cpp#L42-L60) 中 `PtrConversion` 与 `PtrConversionWithFunctionType` 两个用例的断言。

**待本地验证**：地址的具体数值因每次运行而不同（ASLR），不要写死断言。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ExecutorAddr(uint64_t)` 构造函数要标 `explicit`？如果去掉会怎样？

> **参考答案**：标 `explicit` 是为了禁止 `uint64_t` 隐式转成 `ExecutorAddr`。去掉之后，任何接受 `ExecutorAddr` 的函数也能接受一个裸 `uint64_t`（比如文件大小、循环计数器），从而把语义无关的数字当成地址传入，引发隐蔽 bug。`explicit` 强制调用者写明「我要把这个数字当地址」。

**练习 2**：阅读 [ExecutorAddress.h:166-182](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L166-L182)，说出 `ExecutorAddr - ExecutorAddr` 与 `ExecutorAddr + ExecutorAddrDiff` 各自的返回类型。

> **参考答案**：前者返回 `ExecutorAddrDiff`（偏移量），后者返回 `ExecutorAddr`（新地址）。这种「运算结果类型反映语义」正是自定义类型的价值所在——裸 `uint64_t` 做不到。

---

### 4.2 ExecutorAddrRange：区间运算

#### 4.2.1 概念说明

单个地址只能定位「一个字节」，但 JIT 代码是以**段（segment）**为单位加载的：一段只读数据、一段可执行代码、一段可读写数据。要描述「这块内存从哪到哪」，就需要区间类型 `ExecutorAddrRange`。

它的核心约定是 **半开区间 `[Start, End)`**：包含 `Start`，不包含 `End`。这是 C/C++ 世界里最常见的区间约定（和指针区间 `[begin, end)`、Python 的 `range` 一致），好处是：

- 区间长度 = `End - Start`，无需 +1 或 −1。
- 两个相邻区间 `[A, B)` 和 `[B, C)` 首尾相接，`B` 只属于后者，没有歧义。
- 空区间（`Start == End`）自然成立。

#### 4.2.2 核心流程

`ExecutorAddrRange` 提供三类运算：

```
   构造                          判定                         视图
┌────────────┐   ┌──────────────────────────────┐   ┌──────────────┐
│ (Start,End)│   │ empty() / size()             │   │ toSpan<T>()  │
│ (Start,Size)│  │ contains(Addr) / contains(R) │   │ → span<T>    │
│            │   │ overlaps(R)                  │   │              │
└────────────┘   └──────────────────────────────┘   └──────────────┘
```

`contains` 与 `overlaps` 的语义差别是本模块重点，下面用一条数轴说明（摘自测试注释）：

```
     0  1  2
     |  |  |
R0: [#]       -- 在 R1 之前
R1:    [#]    -- [1,2)
R2:       [#] -- 在 R1 之后
R3: [# #]     -- 与 R1 起点重叠
R4:    [# #]  -- 与 R1 终点重叠
```

- `contains(Addr)`：地址是否落在 `[Start, End)` 内（左闭右开）。
- `contains(Range)`：另一个区间是否**完全**被本区间包住。
- `overlaps(Range)`：两个区间是否有任何重叠部分。

#### 4.2.3 源码精读

**结构定义与构造**（[ExecutorAddress.h:184-191](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L184-L191)）：

```cpp
struct ExecutorAddrRange {
  constexpr ExecutorAddrRange() noexcept = default;
  constexpr ExecutorAddrRange(ExecutorAddr Start, ExecutorAddr End) noexcept
      : Start(Start), End(End) {}
  constexpr ExecutorAddrRange(ExecutorAddr Start,
                              ExecutorAddrDiff Size) noexcept
      : Start(Start), End(Start + Size) {}
  ...
  ExecutorAddr Start;
  ExecutorAddr End;
};
```

第二个构造函数接收「起点 + 长度」，内部把它换算成「起点 + 终点」：`End = Start + Size`。注意这里 `Start + Size` 正好用到了上一模块讲的 `ExecutorAddr + ExecutorAddrDiff` 运算——模块之间的依赖在这里体现。

**size 与 empty**（[ExecutorAddress.h:193-194](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L193-L194)）：

```cpp
constexpr bool empty() const noexcept { return Start == End; }
constexpr ExecutorAddrDiff size() const noexcept { return End - Start; }
```

`size()` 直接用 `End - Start`，这正是半开区间约定带来的简洁——没有 +1/-1 的修正。

**contains**（[ExecutorAddress.h:204-209](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L204-L209)）：

```cpp
constexpr bool contains(ExecutorAddr Addr) const noexcept {
  return Start <= Addr && Addr < End;
}
constexpr bool contains(const ExecutorAddrRange &Other) const noexcept {
  return (Other.Start >= Start && Other.End <= End);
}
```

第一个重载里 `Addr < End`（严格小于）正是「右开」的体现：终点 `End` 本身不算在内。测试 [ExecutorAddressTest.cpp:96-98](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ExecutorAddressTest.cpp#L96-L98) 验证了这一点：区间 `[1,2)` 包含地址 `1`，但不包含地址 `2`。

**overlaps**（[ExecutorAddress.h:210-212](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L210-L212)）：

```cpp
constexpr bool overlaps(const ExecutorAddrRange &Other) const noexcept {
  return !(Other.End <= Start || End <= Other.Start);
}
```

这里用了「取反不相交」的写法：两个区间不相交当且仅当「一个完全在另一个之前或之后」，即 `Other.End <= Start`（Other 在我左边）或 `End <= Other.Start`（Other 在我右边）。对其取反就是「相交」。注意又用了 `<=` 而非 `<`，与半开区间一致——`[1,2)` 与 `[2,3)` 不算重叠（共享端点 2，但 2 只属于后者）。

**toSpan**（[ExecutorAddress.h:214-218](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L214-L218)）：

```cpp
template <typename T> constexpr span<T> toSpan() const noexcept {
  assert(size() % sizeof(T) == 0 &&
         "AddressRange is not a multiple of sizeof(T)");
  return span<T>(Start.toPtr<T *>(), size() / sizeof(T));
}
```

`toSpan<T>()` 把一段地址区间重新解释为「连续 `T` 数组」的视图（`span<T>`，见 [u10-l3](u10-l3-core-utilities.md)）。它先断言区间长度是 `sizeof(T)` 的整数倍（否则类型 reinterpret 会越界/错位），再把起点转成 `T*`、元素个数取 `size() / sizeof(T)`。这是 executor 把「我这段内存里放着 N 个某类型对象」交给上层代码使用的标准入口。

#### 4.2.4 代码实践

**实践目标**：构造几个相邻/重叠的区间，验证 `contains` 与 `overlaps` 的行为，亲手确认半开区间约定。

**操作步骤**：

下面这段代码（**示例代码**）完全对应测试 [ExecutorAddressTest.cpp:82-110](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ExecutorAddressTest.cpp#L82-L110) 的 `AddrRanges` 用例，你可以把它放进一个 `main` 或 GoogleTest 里：

```cpp
// 示例代码：验证 ExecutorAddrRange 的区间运算
#include "orc-rt/ExecutorAddress.h"
#include <cassert>

using namespace orc_rt;

int main() {
  ExecutorAddr A0(0), A1(1), A2(2), A3(3);
  ExecutorAddrRange R1(A1, A2);          // [1,2)
  ExecutorAddrRange R3(A0, A2);          // [0,2)
  ExecutorAddrRange R4(A1, A3);          // [1,3)

  // 半开区间：包含起点 1，不包含终点 2
  assert( R1.contains(A1));              // true
  assert(!R1.contains(A2));              // false（终点不含）

  // 「完全包含」要求另一区间整体在本区间内
  assert( R3.contains(R1));              // [0,2) 完全包住 [1,2)
  assert(!R3.contains(R4));              // [0,2) 包不住 [1,3)（3 超出）

  // 重叠判定：共享端点不算重叠
  assert( R3.overlaps(R4));              // [0,2) 与 [1,3) 有 [1,2) 重叠
  ExecutorAddrRange R2(A2, A3);          // [2,3)
  assert(!R1.overlaps(R2));              // [1,2) 与 [2,3) 仅共享端点 2 → 不重叠
  return 0;
}
```

**需要观察的现象**：所有断言均通过，程序正常退出。

**预期结果**：与上述注释一致。如果你把 `!R1.contains(A2)` 改成 `R1.contains(A2)`，断言会失败——这能直观感受「右开」。

**待本地验证**：若你尚未搭好 orc-rt 构建，可先只阅读 `AddrRanges` 测试用例的断言来理解行为，等构建就绪再运行。

#### 4.2.5 小练习与答案

**练习 1**：给定 `ExecutorAddrRange R(ExecutorAddr(100), ExecutorAddrDiff(8))`，求 `R.End` 的值和 `R.size()` 的值。

> **参考答案**：`End = Start + Size = 100 + 8 = 108`；`size() = End - Start = 108 - 100 = 8`。这就是「起点 + 长度」构造函数与半开区间约定的协作结果。

**练习 2**：为什么 `overlaps` 里写的是 `<=` 而不是 `<`？如果把两个 `<=` 都改成 `<` 会发生什么？

> **参考答案**：`<=` 体现了半开区间「端点不计入」的约定：`[1,2)` 和 `[2,3)` 共享端点 2，但 2 只属于后者，所以不算重叠。若改成 `<`，则 `Other.End < Start` 会把「Other 终点正好等于我起点」的情况判为「不相交」的反例之外，导致这两个本不相邻的区间被误判为重叠。

---

### 4.3 Tag / Untag：带标签指针

#### 4.3.1 概念说明

本模块解决一个相对小众但真实存在的问题：**带标签指针（tagged pointer）**。

在某些硬件/操作系统上（典型是 ARM64 的 TBI，Top Byte Ignore），指针的最高 8 位是「空闲」的——CPU 在寻址时会忽略这些位。于是运行时可以把这些位拿来存「标签」：比如调试器用来标记指针类别、GC 用来标记对象颜色、地址消毒剂（ASan/MTE）用来存校验信息。这种指针长这样：

```
高位（标签）                低位（真实地址）
┌────────┬─────────────────────────────────┐
│  0xA5  │  0x0000_0000_0012_3456          │
└────────┴─────────────────────────────────┘
```

问题来了：当 orc-rt 要把这个带标签指针跨进程传递时，**对方进程不一定支持同样的标签约定**。所以通常需要在「打包成 `ExecutorAddr` 之前」把标签剥掉（存纯地址），在「还原成指针之后」再把标签加回去。这就是 `Untag`（解包装）和 `Tag`（再包装）的用途。

> 重要：默认情况下 orc-rt **不**做任何标签处理——`defaultUnwrap`/`defaultWrap` 都是恒等的 `rawPtr`（[ExecutorAddress.h:32-40](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L32-L40)）。`Tag`/`Untag` 是「可选的、需要调用方显式传入」的策略。如果你不在带标签指针的平台上工作，了解默认恒等即可。

#### 4.3.2 核心流程

`Tag` 和 `Untag` 都是**函数对象（functor）**，分别按位「或」和「与」：

```
Untag（打包前剥标签）：
   带标签指针 P  ──(P & UntagMask)──►  纯地址指针  ──fromPtr──►  ExecutorAddr

Tag（还原后加标签）：
   ExecutorAddr  ──toPtr──►  纯地址指针  ──(P | TagMask)──►  带标签指针
```

对应的位运算：

- `Tag`：\( P' = P \mid \text{TagMask} \)，其中 \(\text{TagMask} = \text{TagValue} \ll \text{TagOffset}\)。
- `Untag`：\( P' = P \mathbin{\&} \text{UntagMask} \)，其中 \(\text{UntagMask} = \sim\big(((1 \ll \text{TagLen}) - 1) \ll \text{TagOffset}\big) \)。

`UntagMask` 是「把指定长度、指定位置的若干位清零」的掩码：先构造「这些位全 1、其余位全 0」的掩码 \(((1 \ll \text{TagLen}) - 1) \ll \text{TagOffset}\)，再取反得到「这些位全 0、其余位全 1」，与指针按位与即可清掉标签位。

#### 4.3.3 源码精读

**rawPtr：恒等策略**（[ExecutorAddress.h:31-40](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L31-L40)）：

```cpp
template <typename T> struct rawPtr {
  T *operator()(T *p) const { return p; }
};
template <typename T> using defaultWrap = rawPtr<T>;
template <typename T> using defaultUnwrap = rawPtr<T>;
```

这就是「什么都不做」的默认包装/解包装函数：传入指针，原样返回。

**Tag：按位或加标签**（[ExecutorAddress.h:42-55](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L42-L55)）：

```cpp
class Tag {
public:
  constexpr Tag(uintptr_t TagValue, uintptr_t TagOffset)
      : TagMask(TagValue << TagOffset) {}

  template <typename T> constexpr T *operator()(T *P) {
    return reinterpret_cast<T *>(reinterpret_cast<uintptr_t>(P) | TagMask);
  }

private:
  uintptr_t TagMask;
};
```

构造时直接把「值 << 偏移」预算好存进 `TagMask`，调用时与指针按位或。注意它存的是**已经移位后的掩码**，所以构造后调用是单条 `or` 指令，开销极低。

**Untag：按位与剥标签**（[ExecutorAddress.h:57-70](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorAddress.h#L57-L70)）：

```cpp
class Untag {
public:
  constexpr Untag(uintptr_t TagLen, uintptr_t TagOffset)
      : UntagMask(~(((uintptr_t(1) << TagLen) - 1) << TagOffset)) {}

  template <typename T> constexpr T *operator()(T *P) {
    return reinterpret_cast<T *>(reinterpret_cast<uintptr_t>(P) & UntagMask);
  }

private:
  uintptr_t UntagMask;
};
```

`Untag` 的构造参数是「标签位数 `TagLen`」和「标签起始位 `TagOffset`」，而不是像 `Tag` 那样的「标签值」，因为它只需要知道「哪些位要清掉」，而不关心这些位原来的值。

**端到端往返**：把 `Untag` 传给 `fromPtr`、把 `Tag` 传给 `toPtr`，就能在带标签指针与纯地址之间无损往返。测试 [ExecutorAddressTest.cpp:62-80](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/ExecutorAddressTest.cpp#L62-L80) 的 `WrappingAndUnwrapping` 用例完整演示了这一点：

```cpp
constexpr uintptr_t RawAddr = 0x123456;
int *RawPtr = (int *)RawAddr;

constexpr uintptr_t TagOffset = 8 * (sizeof(uintptr_t) - 1); // 最高字节
uintptr_t TagVal = 0xA5;
uintptr_t TagBits = TagVal << TagOffset;
void *TaggedPtr = (void *)((uintptr_t)RawPtr | TagBits);      // 带标签指针

// 打包前剥掉标签 → EA 里只存纯地址 0x123456
ExecutorAddr EA =
    ExecutorAddr::fromPtr(TaggedPtr, ExecutorAddr::Untag(8, TagOffset));
EXPECT_EQ(EA.getValue(), RawAddr);

// 还原后再加回同样的标签 → 得回原来的带标签指针
void *ReconstitutedTaggedPtr =
    EA.toPtr<void *>(ExecutorAddr::Tag(TagVal, TagOffset));
EXPECT_EQ(TaggedPtr, ReconstitutedTaggedPtr);
```

读这段测试时注意 `TagOffset = 8 * (sizeof(uintptr_t) - 1)`：64 位平台上 `sizeof(uintptr_t) == 8`，所以 `TagOffset = 56`，即把标签放在最高字节（第 56–63 位），正好对应 ARM64 TBI 的「顶字节」。

#### 4.3.4 代码实践

**实践目标**：亲手构造一个带标签指针，验证 `Untag` 能把它还原成纯地址、`Tag` 能把纯地址再变回带标签指针。

**操作步骤**：把上面 `WrappingAndUnwrapping` 用例改写成可在 `main` 里运行的断言（**示例代码**）：

```cpp
// 示例代码：验证 Tag / Untag 往返
#include "orc-rt/ExecutorAddress.h"
#include <cassert>
#include <cstdint>

using namespace orc_rt;

int main() {
  constexpr uintptr_t RawAddr = 0x123456;
  int *RawPtr = (int *)RawAddr;

  constexpr uintptr_t TagOffset = 8 * (sizeof(uintptr_t) - 1); // 最高字节
  uintptr_t TagVal = 0xA5;
  uintptr_t TagBits = TagVal << TagOffset;
  void *TaggedPtr = (void *)((uintptr_t)RawPtr | TagBits);

  // 用 Untag 打包：EA 只应保存纯地址
  ExecutorAddr EA =
      ExecutorAddr::fromPtr(TaggedPtr, ExecutorAddr::Untag(8, TagOffset));
  assert(EA.getValue() == RawAddr);

  // 用 Tag 还原：应得回原始的带标签指针
  void *Back = EA.toPtr<void *>(ExecutorAddr::Tag(TagVal, TagOffset));
  assert(Back == TaggedPtr);
  return 0;
}
```

**需要观察的现象**：两条断言都通过。

**预期结果**：与测试断言一致；`EA.getValue()` 恰好等于 `0x123456`（标签被剥掉），还原后的指针恰好等于原始 `TaggedPtr`（标签被加回）。

**待本地验证**：标签放在最高字节，只有在支持 TBI 的平台上解引用 `TaggedPtr` 才安全；本实践只做位运算比较、不解引用，所以在任意 64 位平台上都能运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么默认的 `defaultUnwrap`/`defaultWrap` 是 `rawPtr`（恒等），而不是 `Untag`/`Tag`？

> **参考答案**：带标签指针是平台相关的少数场景（如 ARM64 TBI）。orc-rt 要在所有平台上工作，必须把标签处理设为「可选策略」：默认恒等，不改变任何指针；只有运行在带标签指针平台、且确实需要剥/加标签的调用方，才显式传入 `Untag`/`Tag`。这体现了「不为用不到的特性付代价」的设计原则。

**练习 2**：`Untag(8, 56)` 在 64 位平台上构造出的 `UntagMask` 是多少（用二进制描述）？

> **参考答案**：先算标签位掩码 \(((1 \ll 8) - 1) \ll 56 = 0xFF00000000000000\)（最高 8 位为 1，其余为 0），取反得 `UntagMask = 0x00FFFFFFFFFFFFFF`（最高 8 位为 0，其余为 1）。与指针按位与即可清除最高字节。

---

## 5. 综合实践

把三个模块串起来，完成下面这个「迷你内存登记表」任务，模拟 executor 端记录一段 JIT 内存的最小场景。

**任务**：实现一个函数 `registerSegment`，它接收

- 一段数据指针 `void *Ptr`，
- 数据长度 `size_t N`（字节数），
- 一个可选的标签值 `uintptr_t TagVal`（默认 0，表示不带标签），

并完成：

1. 用 `ExecutorAddr::fromPtr`（带 `Untag` 或恒等）把 `Ptr` 打包成 `ExecutorAddr Start`。
2. 用 `ExecutorAddrRange(Start, ExecutorAddrDiff(N))` 构造区间 `R`。
3. 用 `R.toSpan<char>()` 取出 `span<char>`，验证其 `size()` 等于 `N`。
4. 返回 `R`，并保证：从 `R.Start.toPtr<void*>()`（必要时配 `Tag`）能还原回与传入 `Ptr` 相等的指针。

**参考实现框架**（**示例代码**）：

```cpp
#include "orc-rt/ExecutorAddress.h"
#include <cassert>
#include <cstddef>
#include <cstdint>

using namespace orc_rt;

ExecutorAddrRange registerSegment(void *Ptr, size_t N, uintptr_t TagVal = 0) {
  constexpr uintptr_t TagOffset = 8 * (sizeof(uintptr_t) - 1);
  // 有标签就用 Untag 剥掉，没标签就恒等
  ExecutorAddr Start = (TagVal != 0)
      ? ExecutorAddr::fromPtr(Ptr, ExecutorAddr::Untag(8, TagOffset))
      : ExecutorAddr::fromPtr(Ptr);

  ExecutorAddrRange R(Start, ExecutorAddrDiff(N));
  auto S = R.toSpan<char>();
  assert(static_cast<size_t>(S.size()) == N);

  // 还原检查
  void *Back = (TagVal != 0)
      ? Start.toPtr<void *>(ExecutorAddr::Tag(TagVal, TagOffset))
      : Start.toPtr<void *>();
  assert(Back == Ptr);
  return R;
}
```

**验证**：准备一个 `char buf[16]`，调用 `registerSegment(buf, 16)`，断言返回区间 `contains(ExecutorAddr::fromPtr(buf))` 为真、`size()` 为 16。再尝试构造两个相邻区间并验证 `overlaps`。

**待本地验证**：标签分支仅在带标签指针平台上有意义，普通平台用默认 `TagVal = 0` 走恒等路径即可。

## 6. 本讲小结

- `ExecutorAddr` 是「类型安全的地址整数」：内部存 `uint64_t`（可跨进程序列化），但用 `explicit` 构造、`fromPtr`/`toPtr` 显式转换、以及「地址 ± 偏移」的严格运算符，杜绝裸 `uint64_t` 的语义误用。
- `fromPtr` 通过「指针 → `uintptr_t` → `uint64_t`」两段转换保证无损；`toPtr` 用 SFINAE 区分「对象指针」与「函数类型」两条还原路径，并用 `assert` 防止窄平台越界。
- `ExecutorAddrRange` 是半开区间 `[Start, End)`：`size() = End - Start` 无需修正；`contains` 左闭右开、`overlaps` 用「取反不相交」实现且共享端点不算重叠。
- `toSpan<T>()` 把地址区间重新解释为 `span<T>` 视图，是 executor 把内存交给上层使用的标准入口，带「长度必须是 `sizeof(T)` 整数倍」的断言保护。
- `Tag`/`Untag` 是可选的「带标签指针」策略：`Tag` 按位或加标签、`Untag` 按位与剥标签；默认 `rawPtr` 恒等，只有显式传入才生效。
- 这些类型是 orc-rt 全库的「地址语言」：后续 Service（内存映射、动态库查找）、序列化（SPS）、跨进程调用（ControllerAccess）都会反复用到它们。

## 7. 下一步学习建议

掌握了「地址」这个基本词汇后，建议：

1. 阅读 [include/orc-rt/SimplePackedSerialization.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h) 中的 `SPSExecutorAddr` / `SPSExecutorAddrRange` 序列化特化，看 `ExecutorAddr` 如何被压成字节流跨进程传输（对应 [u6-l1](u6-l1-simple-packed-serialization.md)）。
2. 阅读 [lib/executor/SimpleNativeMemoryMap.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/SimpleNativeMemoryMap.cpp)，看 `ExecutorAddrRange` 如何在真实的内存分配 Service 里表示一段段 JIT 内存（对应 [u7-l3](u7-l3-simple-native-memory-map.md)）。
3. 在阅读前，可先看 [u2-l3](u2-l3-error-model-overview.md) 补齐错误处理模型，因为上述 Service 的几乎所有接口都返回 `Expected<T>`。
