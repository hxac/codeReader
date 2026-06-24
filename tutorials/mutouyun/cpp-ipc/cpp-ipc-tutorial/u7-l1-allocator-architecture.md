# 分配器架构：类型擦除与对齐分配

## 1. 本讲目标

本讲进入 libipc 的「内存管理子系统」（`mem/` 目录），建立**分配器整体架构**的认知。学完后你应当能够：

- 说清 `ipc::mem` 内部三层分配抽象各自的角色：**接口约定**（鸭子类型的「memory resource」）、**资源实现**（`new_delete_resource`，跨平台对齐分配的兜底）、**多态分配器**（`bytes_allocator`，类型擦除的统一句柄）。
- 理解 `new_delete_resource` 如何用一串编译期 `#if` 在 C++17 `std::aligned_alloc`、Windows `_aligned_malloc`、POSIX `posix_memalign`、普通 `malloc` 之间做平台分派，并用 `round_up` 满足 `aligned_alloc` 的尺寸约束。
- 掌握 `bytes_allocator` 用 `holder_mr` 类族 + 内联小缓冲（SBO）实现**无继承的类型擦除**——任何带 `allocate`/`deallocate` 方法、满足鸭子签名的类型都能被它持有，无需继承任何基类。
- 对比 libipc 的 `bytes_allocator` 与标准库 `std::pmr::memory_resource` 的关键差异，理解作者为何自造一套而非直接用 `std::pmr`。

本讲只讲「架构骨架」与最底层资源。`monotonic_buffer_resource`（单调缓冲）、`block_pool`（空闲链表）、`central_cache`（中央缓存）等更上层的分配策略留待 u7-l2、u7-l3 展开。

## 2. 前置知识

在进入源码前，先用通俗语言讲清三个概念。

### 2.1 为什么要自己造分配器？

C++ 标准库已经有 `new`/`delete`、`malloc`/`free`、`std::allocator`、C++17 的 `std::pmr`，为什么 libipc 还要造一套？因为 libipc 作为一个追求**低延迟、零拷贝**的进程间通信库，对内存有三类特殊诉求：

1. **对齐分配**：缓存行对齐（防伪共享，见 u8-l1）、按 1KB 对齐的大消息 chunk（见 u3-l3），要求分配器能按任意 2 的幂对齐返回指针。
2. **多态替换**：不同子系统想用不同策略（系统堆、单调缓冲、空闲链表池）而**不改调用代码**——这是「策略模式」需求，需要一个类型擦除的多态分配器接口。
3. **跨平台一致**：同一份代码要在 Linux/Windows/FreeBSD 上拿到对齐指针，但三个平台的对齐分配系统调用完全不同，需要一层抽象。

`bytes_allocator` + 「memory resource」接口约定正是为此而生。

### 2.2 什么是「类型擦除」（type erasure）？

类型擦除是一种设计手法：**让一个对象的类型在运行时携带不同的实现，而其静态类型保持统一**。典型例子是 `std::function`——它能装下任意签名匹配的可调用对象（函数指针、lambda、仿函数），但它的静态类型只是 `std::function<...>`，调用方看不到被装对象的真实类型。

实现类型擦除的标准套路是「**虚函数 + 一个隐藏的实现类**」：

- 定义一个带纯虚函数的接口类（类型擦除后的统一面貌）；
- 对每个真实类型，写一个派生类把真实类型「包」起来，并实现虚函数转发到真实类型；
- 接口类用基类指针调用虚函数，于是真实类型被「擦除」了。

`bytes_allocator` 正是用这个套路持有任意的 memory resource，但它做得很巧妙：擦除用的 holder 对象**内联**存放在 `bytes_allocator` 自己的字节数组里（小缓冲优化 SBO），而不是堆分配。

### 2.3 什么是「鸭子类型」（duck typing）与 SFINAE？

「如果一个东西走起来像鸭子、叫起来像鸭子，那它就是鸭子。」C++ 在编译期也能玩鸭子类型：**不要求类型继承某个基类，只要求它有特定签名的方法**。

判定「某个类型是否有 `allocate` 方法」靠的是 SFINAE（Substitution Failure Is Not An Error，替换失败不是错误）：写一个模板，在替换实参时如果表达式非法，编译器不会报错，而是把这个重载**静默剔除**。libipc 用 `std::enable_if` + `decltype` 把「能否调用 `allocate`/`deallocate`」转化为一个编译期布尔，进而用模板偏特化选择不同实现。

> 承接 u3-l3：那里讲到的大消息 chunk 用 `recycle_storage` 作为 `buff_t` 析构器、按 1KB 对齐回收内存——这条路径最终都要落到「按对齐分配一块内存」的本原语。本讲就拆开这个本原语。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 角色 | 关键内容 |
|------|------|----------|
| [include/libipc/mem/memory_resource.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/memory_resource.h) | 资源声明 | 声明 `new_delete_resource` 与 `monotonic_buffer_resource` 两个资源类 |
| [include/libipc/mem/bytes_allocator.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h) | 类型擦除分配器（头文件） | `bytes_allocator` 类、`holder_mr` 类型擦除类族、鸭子类型 trait |
| [src/libipc/mem/bytes_allocator.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/bytes_allocator.cpp) | 类型擦除分配器（实现） | `get_holder`、`init_default_resource`、`allocate`/`deallocate` 运行时转发 |
| [src/libipc/mem/new_delete_resource.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp) | 跨平台对齐分配（实现） | `aligned_alloc`/`_aligned_malloc`/`posix_memalign` 平台分派 |
| [src/libipc/mem/verify_args.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/verify_args.h) | 参数校验 | `verify_args(bytes, alignment)` 检查非零且对齐为 2 的幂 |
| [include/libipc/imp/aligned.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/aligned.h) | 对齐工具 | `round_up(value, alignment)` 向上取整到对齐倍数 |
| [test/mem/test_mem_bytes_allocator.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_bytes_allocator.cpp) | 单元测试 | 用 `dummy_resource` 验证类型擦除、`sizeof` 断言 |
| [test/mem/test_mem_memory_resource.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_memory_resource.cpp) | 单元测试 | 验证 `new_delete_resource` 的对齐返回与非法参数拒绝 |

三层关系一句话概括：**`bytes_allocator`（分配器）持有任意「memory resource」（资源），`new_delete_resource` 是最底层、最通用的资源实现**。`bytes_allocator` 是「使用方」面向的类型，`new_delete_resource` 是「被持有方」面向的类型。

---

## 4. 核心概念与源码讲解

### 4.1 memory_resource 接口约定：鸭子类型与 is_memory_resource

#### 4.1.1 概念说明

libipc **没有一个叫 `memory_resource` 的基类**（注意！）。它只有一个**接口约定**：任何类型只要提供了符合签名的 `allocate` 和 `deallocate` 两个成员函数，就被认定为「memory resource」，可以被 `bytes_allocator` 持有。这是一种**鸭子类型**约定，靠编译期 trait 判定，而非继承关系。

这正是作者在头文件注释里强调的要点——与 `std::pmr` 不同，它「**不依赖特定的继承关系**，只约束传入对象的行为接口与 `std::pmr::memory_resource` 一致」。详见 [bytes_allocator.h:L49-L60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L49-L60)。

#### 4.1.2 核心流程

判定一个类型 `T` 是否是 memory resource，分三步：

1. **`has_allocate<T>`**：判定 `T` 是否有一个可调用的 `allocate(size_t, size_t)` 且返回值能转成 `void*`。用 `std::enable_if` + `decltype` 做表达式合法性探测，合法则继承 `std::true_type`，否则（默认主模板）为 `std::false_type`。
2. **`has_deallocate<T>`**：判定 `T` 是否有 `deallocate(void*, size_t, size_t)`（只看表达式是否合法，不限返回类型）。
3. **`is_memory_resource<T>`**：当且仅当 `has_allocate && has_deallocate` 都为真时，`is_memory_resource<T>` 通过 `enable_if_t` 求值为 `bool`；否则是替换失败（SFINAE），`is_memory_resource<T>` 根本不存在。

这个 `is_memory_resource` 随后会被用作模板偏特化的「开关」（见 4.3）——这正是「鸭子类型」转化为「编译期分派」的桥梁。

#### 4.1.3 源码精读

判定 `has_allocate` 的核心（探测方法存在并要求返回 `void*`）：

[bytes_allocator.h:L24-L32](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L24-L32) —— 用 `decltype(std::declval<T&>().allocate(...))` 探测 `T` 是否有 `allocate` 方法，并要求其返回值能 `is_convertible` 到 `void*`；合法则偏特化为 `std::true_type`。

`is_memory_resource` 把两个 trait 合并为一个 enable_if 别名：

[bytes_allocator.h:L44-L47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L44-L47) —— `is_memory_resource<T>` = `enable_if_t<has_allocate && has_deallocate, bool>`，资源合法时求值为 `bool`，非法时为替换失败。

整段 trait 定义见 [bytes_allocator.h:L22-L47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L22-L47)。

注意 `new_delete_resource` 本身就是按这个约定设计的——它恰好有 `allocate(bytes, alignment) -> void*` 与 `deallocate(p, bytes, alignment)` 两个方法，所以天然满足 `is_memory_resource`，无需继承任何东西。它的声明见 [memory_resource.h:L25-L38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/memory_resource.h#L25-L38)。

#### 4.1.4 代码实践

**实践目标**：亲手验证「鸭子类型」判定——确认 `int`、`std::vector`、`std::allocator` 都**不是** memory resource，而带 `allocate`/`deallocate` 的自定义类**是**。

**操作步骤**：阅读测试 [test_mem_bytes_allocator.cpp:L40-L58](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_bytes_allocator.cpp#L40-L58)。测试里的 `dummy_resource`（只有两个返回 `nullptr`/空操作的成员方法）被当作合法资源使用：

[test_mem_bytes_allocator.cpp:L26-L36](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_bytes_allocator.cpp#L26-L36) —— `dummy_resource` 既不继承任何东西、`allocate` 还返回 `nullptr`，但因为方法签名对得上，`is_memory_resource` 判它为真，`bytes_allocator` 照样能持有它。

**需要观察的现象**：在 `TEST(bytes_allocator, memory_resource_traits)` 里，`has_allocate<void/int/std::vector<int>>::value` 全为 `false`，因为这些类型没有 `allocate` 方法。

**预期结果**：`bytes_allocator{&dummy_res}` 能编译通过（说明 `dummy_resource` 被判为合法资源），但 `alc2.allocate(128) == nullptr`（因为 dummy 的 allocate 恒返回 nullptr）——见 [test_mem_bytes_allocator.cpp:L67](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_bytes_allocator.cpp#L67)。具体运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果给 `dummy_resource` 的 `allocate` 改成返回 `int`（而非 `void*`），`is_memory_resource<dummy_resource>` 还成立吗？

**答案**：不成立。`has_allocate` 用 `std::is_convertible<返回值, void*>` 做约束（[bytes_allocator.h:L29-L31](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L29-L31)），`int` 不能隐式转成 `void*`，故偏特化失败，回落到主模板 `std::false_type`，于是 `is_memory_resource` 替换失败，`bytes_allocator{&dummy}` 编译报错。

**练习 2**：`has_deallocate` 为什么不像 `has_allocate` 那样检查返回类型？

**答案**：因为 `deallocate` 的语义就是「释放、无返回值」（[bytes_allocator.h:L37-L42](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L37-L42) 只用 `decltype(...)` 检查表达式合法、不约束返回类型），任何返回类型（`void` 或别的）都可接受，只关心这个调用能否成立。

---

### 4.2 new_delete_resource：跨平台对齐分配

#### 4.2.1 概念说明

`new_delete_resource` 是 libipc 最底层的资源实现——它把请求转给**系统堆**，但保证按指定对齐返回指针。它是所有更高级策略（单调缓冲、池）最终回落到的「兜底资源」，也是 `bytes_allocator` 默认构造时绑定的资源。

它的难点在于**跨平台**：拿到一个「大小 + 对齐」请求，Linux/Windows/老编译器各自有不同的系统调用，且这些调用的参数语义还不一样（例如 C++17 的 `std::aligned_alloc` 要求大小是 alignment 的整数倍）。`new_delete_resource::allocate` 用一串编译期 `#if` 把这些差异抹平。

#### 4.2.2 核心流程

`allocate(bytes, alignment)` 的分派逻辑（伪代码）：

```
若 verify_args(bytes, alignment) 失败 → 返回 nullptr      // 非 0 且对齐为 2 的幂
否则按编译期条件选择后端：
  if (C++17 且 非 MSVC 且 非 MinGW 且 非 WebOS):
      大小向上取整到 alignment 倍数 → std::aligned_alloc(alignment, 取整后大小)
  else if (alignment <= max_align_t):  std::malloc(bytes)
  else if (Windows):                   ::_aligned_malloc(bytes, alignment)
  else (POSIX):                        ::posix_memalign(&p, alignment, bytes)
```

**为什么 C++17 分支要把大小向上取整？** 因为 `std::aligned_alloc` 的标准要求「size 必须是 alignment 的整数倍」，否则行为未定义。libipc 用 `round_up` 把 `bytes` 拉到 alignment 的倍数：

\[ \text{round\_up}(v, a) = (v + a - 1)\ \&\ \sim(a - 1) \]

当 `a` 是 2 的幂时，`a-1` 是低位全 1 的掩码，`~(a-1)` 把这些低位清零——这是经典的「向上取整到 2 的幂倍数」位运算技巧。实现见 [aligned.h:L69-L72](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/aligned.h#L69-L72)。

`deallocate` 是对称的：C++17 统一用 `std::free`；否则按平台用 `_aligned_free`（Windows）或 `free`（POSIX）。`max_align_t` 以下对齐的指针直接 `std::free`，因为普通 `malloc` 已保证 `max_align_t` 对齐。

#### 4.2.3 源码精读

`new_delete_resource::get()` 返回一个函数内静态单例（线程安全初始化）：

[new_delete_resource.cpp:L19-L22](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L19-L22) —— `static new_delete_resource mem_res;` 利用 C++11 保证的静态局部变量线程安全初始化，让 `get()` 成为获取默认资源的统一入口。这也保证它的生命期覆盖整个程序，绝不会先于任何 `bytes_allocator` 析构（见 4.3.5 练习 1）。

`allocate` 的平台分派（关键四分支）：

[new_delete_resource.cpp:L33-L64](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L33-L64) —— 这是本模块的核心。四个分支：

- [L39-L42](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L39-L42)：C++17 优先走 `std::aligned_alloc`，并用 `round_up(bytes, alignment)` 保证大小是对齐倍数。
- [L44-L47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L44-L47)：对齐不超过 `max_align_t` 时普通 `malloc` 就够（它本就保证最大标量对齐）。
- [L48-L50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L48-L50)：Windows 走 CRT 的 `_aligned_malloc`。
- [L51-L62](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L51-L62)：其余 POSIX 平台走 `posix_memalign`，失败时记录错误日志并返回 nullptr。

参数校验 `verify_args`：

[verify_args.h:L11-L13](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/verify_args.h#L11-L13) —— `(bytes > 0) && (alignment > 0) && ((alignment & (alignment-1)) == 0)`，其中 `(alignment & (alignment-1)) == 0` 是判定「2 的幂」的经典位运算。

`deallocate` 的对称分派见 [new_delete_resource.cpp:L74-L98](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L74-L98)，注意它先判 `p == nullptr` 直接返回（早退省事）。

#### 4.2.4 代码实践

**实践目标**：验证 `new_delete_resource::allocate` 返回的指针确实满足请求的对齐，且非法参数返回 `nullptr`。

**操作步骤**：阅读并运行测试 [test_mem_memory_resource.cpp:L24-L40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_memory_resource.cpp#L24-L40)。该测试用辅助函数 `test_mr` 分配后断言 `(std::size_t)p % alignment == 0`（指针地址能被对齐值整除）。

**需要观察的现象**：

- `test_mr(mem_res, 1, 3)` 返回 `nullptr`——因为 `alignment=3` 不是 2 的幂，被 `verify_args` 拒绝。
- `test_mr(mem_res, 1, 64)` 返回非空且地址是 64 的倍数——验证了真正的对齐分配生效。

**预期结果**：`bytes=0` 或 `alignment` 非 2 的幂时返回 `nullptr`；合法请求返回的指针地址能被 alignment 整除。具体运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：在 C++17、非 Windows 平台上，请求 `allocate(10, 16)` 实际向系统申请了多少字节？为什么？

**答案**：申请了 `round_up(10, 16) = (10 + 15) & ~15 = 16` 字节（[new_delete_resource.cpp:L42](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L42)）。因为 `std::aligned_alloc` 标准要求 size 是 alignment 的整数倍，10 不是 16 的倍数，故向上取整到 16。

**练习 2**：为什么 `alignment <= alignof(std::max_align_t)` 时直接用 `malloc` 就够了？

**答案**：`malloc` 返回的指针保证满足「最大标量类型」（`max_align_t`，通常 16 字节）的对齐。所以请求的对齐若不超过它，`malloc` 已天然满足，无需更重的对齐分配接口（[new_delete_resource.cpp:L44-L47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L44-L47)）。

---

### 4.3 bytes_allocator：类型擦除与小缓冲优化（SBO）

#### 4.3.1 概念说明

`bytes_allocator` 是使用方真正面向的多态分配器。它的「多态」体现在：**构造时绑定哪个资源，分配就走哪条路**——绑 `new_delete_resource` 走系统堆，绑 `monotonic_buffer_resource` 走单调缓冲，绑你自己写的资源走你的逻辑。而这一切对调用方而言只是 `bytes_allocator alc; alc.allocate(n)`。

关键设计有两点：

1. **类型擦除**：`bytes_allocator` 不知道、也不需要知道所持资源的真实类型。它通过一个虚函数接口类 `holder_mr_base` 持有资源，运行时靠虚分派转发 `alloc`/`dealloc`。被擦除的是「资源的具体类型」。
2. **小缓冲优化（SBO）**：擦除用的 holder 对象**不堆分配**，而是内联存在 `bytes_allocator` 自己的一个定长字节数组 `holder_` 里。这让 `sizeof(bytes_allocator)` 固定为两个指针大小（16 字节，64 位下），拷贝/移动就是按位拷这个数组，零堆开销。

这两点合起来，使 `bytes_allocator` 既能多态、又轻量得像个普通值类型。

#### 4.3.2 核心流程

类型擦除类族由三个模板/类构成，呈现「接口 → 持有基 → 转发派生」三层：

```
holder_mr_base                      (非模板，纯虚接口：virtual alloc/dealloc + 虚析构)
      ▲
      │ public
holder_mr<MR, U>                    (主模板：持有 MR* res_，alloc 返 nullptr、dealloc 空操作 ——「占位」行为)
      ▲
      │ public   仅当 MR 满足 is_memory_resource 时偏特化命中
holder_mr<MR, is_memory_resource<MR>>   (偏特化：继承主模板拿 res_，重写 alloc/dealloc 转发到 res_->allocate/deallocate)
```

构造流程：

1. `bytes_allocator{&resource}` 调用模板构造函数（[bytes_allocator.h:L134-L141](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L134-L141)）。
2. 若指针为 `nullptr`，转而调 `init_default_resource()` 绑定默认的 `new_delete_resource`。
3. 否则用 placement new 把一个 `holder_mr<T>` **直接构造进**内联数组 `holder_` 里（`ipc::construct<holder_mr<T>>(holder_.data(), p_mr)`）。
4. 此后 `bytes_allocator::allocate(s,a)` 调 `get_holder().alloc(s,a)`，经虚分派落到具体 holder 的 `alloc`，再转发到 `res_->allocate(s,a)`。

**偏特化如何命中**：类模板声明为 `template <typename MR, typename = bool> class holder_mr;`，第二个参数默认是 `bool`。当你写 `holder_mr<T>` 时它展开成 `holder_mr<T, bool>`；对合法资源 `T`，`is_memory_resource<T>` 恰好求值为 `bool`，于是偏特化 `holder_mr<MR, is_memory_resource<MR>>` = `holder_mr<T, bool>` 匹配胜出，走「转发」版；对 `void*` 这类非资源，`is_memory_resource<void*>` 是替换失败，偏特化无法实例化，只能用主模板的「空操作」版。

**SBO 的尺寸依据**：内联数组大小取自 `void_holder_t = holder_mr<void *>` 的 `sizeof`（[bytes_allocator.h:L112-L113](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L112-L113)）。`void*` 不是 memory resource（没有 `allocate` 方法），故只能用主模板；主模板布局 = 一个虚表指针 + 一个 `MR* res_` = 两个机器字（16 字节）。而任何合法资源的 holder 都继承自主模板、布局相同，故都能塞进这块定长缓冲。这就是测试里 `sizeof(bytes_allocator) == sizeof(void*)*2` 的由来。

#### 4.3.3 源码精读

类型擦除的接口类与主模板（占位行为）：

[bytes_allocator.h:L63-L89](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L63-L89) —— `holder_mr_base` 定义纯虚 `alloc`/`dealloc`（L66-L67）；主模板 `holder_mr<MR, U>` 持有 `MR* res_`（L80）并把 `alloc` 实现为返回 `nullptr`（L87，注释说明是为绕过 MSVC C2259 抽象类实例化错误）。注意 L77-78 注释明确：当 `MR` 不能转成 memory resource 时，用这个空持有类来计算 holder 的合理尺寸。

转发派生（偏特化，仅对合法资源生效）：

[bytes_allocator.h:L95-L110](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L95-L110) —— 继承 `holder_mr<MR, void>`（即主模板，提供 `res_` 存储），L103-L109 重写 `alloc`/`dealloc` 转发到 `base_t::res_->allocate(s,a)` / `res_->deallocate(p,s,a)`。这是类型擦除「落地」的一步：调用方只见 `holder_mr_base`，实际干活的是这个偏特化。

内联小缓冲与模板构造函数：

[bytes_allocator.h:L112-L141](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L112-L141) —— L113 的 `holder_` 是 `alignas(void_holder_t) std::array<byte, sizeof(void_holder_t)>`，即一块按 holder 对齐、等大的裸字节数组；L134-L141 的模板构造函数用 `is_memory_resource<T>` 做约束，再用 `ipc::construct<holder_mr<T>>` 把 holder **原地构造**进这块数组——没有 `new`，没有堆分配。

运行时转发实现（`.cpp`）：

[bytes_allocator.cpp:L11-L21](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/bytes_allocator.cpp#L11-L21) —— `get_holder` 把 `holder_.data()` reinterpret 成 `holder_mr_base&`（L11-L13）；`init_default_resource`（L19-L21）把默认资源 `new_delete_resource::get()` 也用同样的 placement-construct 塞进 `holder_`——所以默认构造与显式绑定走的是同一条类型擦除路径。

`bytes_allocator::allocate/deallocate` 自身只做对齐幂次校验，然后转发：

[bytes_allocator.cpp:L34-L50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/bytes_allocator.cpp#L34-L50) —— L36/L45 用 `(a & (a-1)) != 0` 校验对齐为 2 的幂，不满足记错误日志并返回；合法则调 `get_holder().alloc(s, a)` / `.dealloc(...)` 经虚函数转到真实资源。

默认构造、析构、swap：

[bytes_allocator.cpp:L23-L32](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/bytes_allocator.cpp#L23-L32) —— 默认构造（L23-L24）委托给 `bytes_allocator(new_delete_resource::get())`；析构（L26-L28）调 `ipc::destroy(&get_holder())` 触发 holder 的**虚析构**清理；`swap`（L30-L32）直接交换两个 `holder_` 字节数组。

`bytes_allocator` 还提供 `construct<T>(args...)` / `destroy(p)` 便捷方法，把「分配内存 + placement new 构造对象」/「调析构 + 释放内存」打包：

[bytes_allocator.h:L149-L159](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L149-L159) —— `construct`（L150-L153）先 `allocate(sizeof(T), alignof(T))` 再 `ipc::construct<T>`；`destroy`（L156-L159）先 `ipc::destroy(p)` 拿回指针再 `deallocate`。注意它**按字节分配**（`allocate(bytes, align)` 返回 `void*`），与 STL 分配器「按对象个数分配」语义不同。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `bytes_allocator` 的类型擦除与 SBO——用同一类型 `bytes_allocator` 持有两种不同资源，观察分配行为不同；并验证其 `sizeof` 只有 2 个指针。

**操作步骤**：

1. 阅读 [test_mem_bytes_allocator.cpp:L60-L80](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_bytes_allocator.cpp#L60-L80) `TEST(bytes_allocator, ctor_copy_move)`：用同一个 `bytes_allocator` 类型分别绑定 `new_delete_resource`（能分配）与 `dummy_resource`（恒返回 nullptr）。
2. 关注三处断言：绑 `new_delete_resource` 的 `alc1.allocate(128)` 非 null（L65）；绑 `dummy_resource` 的 `alc2.allocate(128)` 为 null（L67，类型擦除生效但 dummy 不干活）；拷贝/移动后行为随资源走（L71-L79）。
3. 再看 [test_mem_bytes_allocator.cpp:L101-L103](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_bytes_allocator.cpp#L101-L103) `TEST(bytes_allocator, sizeof)`：断言 `sizeof(bytes_allocator) == sizeof(void*)*2`，证明 SBO 让分配器本身只有 16 字节。

**需要观察的现象**：

- 同一个静态类型 `bytes_allocator`，因为构造时绑定资源不同，`allocate(128)` 一个返回有效指针、一个返回 `nullptr`——这是「类型擦除带来运行时多态」的直接证据。
- 拷贝构造 `alc3{alc1}` 后 `alc3.allocate(128)` 仍能成功——说明 holder 是按 SBO 字节数组复制的，复制后依然指向同一个真实资源。

**预期结果**：上述断言全部通过，且 `sizeof(ipc::mem::bytes_allocator)` 在 64 位平台上等于 16。具体运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`bytes_allocator` 持有的资源指针（`res_`）是它自己拥有的吗？如果资源对象先于 `bytes_allocator` 析构会怎样？

**答案**：不是。`holder_mr` 只**持有指针**、不拥有资源对象（[bytes_allocator.h:L80](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L80) 的 `MR *res_`）。头文件 L133 的注释明确警告：「指针的生命期必须长于 `bytes_allocator`」。若资源先析构，之后再经 `bytes_allocator` 分配就是悬垂指针解引用（未定义行为）。这也是 `new_delete_resource::get()` 用静态单例（[new_delete_resource.cpp:L19-L22](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L19-L22)）的原因——单例生命期覆盖整个程序，绝不可能先于分配器析构。

**练习 2**：为什么偏特化 `holder_mr<MR, is_memory_resource<MR>>` 要继承自主模板 `holder_mr<MR, void>` 而不是直接继承 `holder_mr_base`？

**答案**：为了复用主模板已声明的 `MR *res_` 成员及其构造逻辑。偏特化只负责「把占位的 null 行为重写为转发」（L103-L109），存储仍由主模板提供。这样 holder 布局固定（虚表指针 + `res_`），才能用 `void_holder_t` 的 `sizeof` 统一为所有 holder 预留 SBO 空间。

---

### 4.4 与 std::pmr::memory_resource 的对比

#### 4.4.1 概念说明

C++17 标准库也有一套多态分配器：`std::pmr::memory_resource`（抽象基类）+ `std::pmr::polymorphic_allocator<T>`（持有一个 `memory_resource*` 的 STL 分配器）。既然标准库已有，libipc 为什么自造 `bytes_allocator`？核心差异在「资源如何被认定为合法」以及「分配单元语义」。

#### 4.4.2 核心流程

两者的差异可归纳为四点：

| 维度 | std::pmr::memory_resource | libipc bytes_allocator |
|------|---------------------------|------------------------|
| 资格认定 | **继承**：资源必须 `public` 继承 `std::pmr::memory_resource`，重写 protected `do_allocate`/`do_deallocate` | **鸭子类型**：资源只要有签名匹配的 `allocate`/`deallocate` 即可，零继承，靠 `is_memory_resource` trait 判定 |
| 持有方式 | `polymorphic_allocator` 持一个 `memory_resource*`（纯指针，资源生命期外部） | 持一个**类型擦除的 holder**，且 holder 内联在 SBO 字节数组里（无额外堆分配） |
| 分配单元 | `allocate(n)` 返回 `T*`，按**对象个数**分配（元素类型 `T` 是分配器模板参数） | `allocate(bytes, align)` 返回 `void*`，按**字节数**分配，元素类型与分配器解耦 |
| 便捷构造 | `construct`/`destroy` 由 `polymorphic_allocator` 标准提供 | 自带 `construct<T>(args...)`/`destroy(p)`（[bytes_allocator.h:L150-L159](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L150-L159)），先按字节分配再 placement new |

一句话：std::pmr 是「**继承 + 元素类型模板**」的官方方案；libipc 是「**鸭子类型 + 字节级 + SBO**」的轻量自造方案。后者让任意带 `allocate`/`deallocate` 的类型（包括标准库的 `std::pmr::memory_resource` 本身！）都能被 `bytes_allocator` 持有，因为它也满足鸭子约定。

#### 4.4.3 源码精读

作者在头文件注释里点明了「不依赖继承」这一设计意图：

[bytes_allocator.h:L49-L60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L49-L60) —— 注释直说：与 `std::pmr::container_allocator` 不同，它不依赖特定继承关系，只约束传入对象的接口行为符合 `std::pmr::memory_resource`。

测试侧也试图验证 `std::pmr::memory_resource` 本身能被 `has_allocate` 认作合法资源（因为它有 `allocate` 方法返回 `void*`）：

[test_mem_bytes_allocator.cpp:L45-L48](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_bytes_allocator.cpp#L45-L48) —— 这段断言的意图是确认 `has_allocate<std::pmr::memory_resource>::value` 为真，说明 libipc 的鸭子约定是 std::pmr 的**超集**：pmr 资源天然兼容 libipc 分配器，反之则不行（libipc 自定义资源不必继承 pmr 基类）。

> 说明：该测试片段的宏守卫是 `LIBIMP_CPP_17`（注意是 `LIBIMP` 而非 `LIBIPC`），在本仓库中此宏不会被定义，故这组断言实际不参与编译；具体以**本地编译运行**的实际行为为准。

#### 4.4.4 代码实践

**实践目标**：用「一句话 + 一张表」回答「bytes_allocator 如何在不继承的情况下持有任意 memory_resource，以及它与 std::pmr::memory_resource 的关键差异」。

**操作步骤**：

1. 重读 [bytes_allocator.h:L134-L141](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L134-L141) 的模板构造函数，确认它接受 `T *p_mr` 且用 `is_memory_resource<T>` 做模板约束（而非要求 `T` 继承某基类）。
2. 自己写一个最小资源类（示例代码，**非项目原有代码**）：

```cpp
// 示例代码：演示「不继承任何基类」也能被 bytes_allocator 持有
struct my_logging_resource {
  std::size_t last_bytes = 0;
  void *allocate(std::size_t bytes, std::size_t /*align*/) {
    last_bytes = bytes;
    return ::operator new(bytes);     // 任意实现都行
  }
  void deallocate(void *p, std::size_t, std::size_t) {
    ::operator delete(p);
  }
};

my_logging_resource res;
ipc::mem::bytes_allocator alc{&res};  // 不需要 res 继承任何东西
auto p = alc.allocate(64);
// res.last_bytes 此时应为 64，证明调用真的转到了 my_logging_resource
alc.deallocate(p, 64);
```

**需要观察的现象**：`my_logging_resource` 没有任何基类，但 `bytes_allocator{&res}` 能编译，且 `allocate(64)` 后 `res.last_bytes == 64`——证明类型擦除把调用转发到了这个自定义资源。

**预期结果**：自定义资源被成功持有，转发生效。具体运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：标准库的 `std::pmr::memory_resource` 对象能否被 libipc 的 `bytes_allocator` 直接持有？反过来呢？

**答案**：能。`std::pmr::memory_resource` 有返回 `void*` 的 `allocate(size_t, size_t)` 和 `deallocate(void*, size_t, size_t)`，满足 libipc 鸭子约定，故 `has_allocate` 判真、可被 `bytes_allocator` 持有（见 [test_mem_bytes_allocator.cpp:L45-L48](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_bytes_allocator.cpp#L45-L48) 的设计意图）。反过来不行：libipc 自定义资源（如 `my_logging_resource`）不继承 `std::pmr::memory_resource`，故不能塞进需要继承关系的 `std::pmr::polymorphic_allocator`。可见 libipc 约定是 pmr 的超集。

**练习 2**：同样是「持有一个资源」，std::pmr 的 `polymorphic_allocator` 持的是裸 `memory_resource*`，libipc 的 `bytes_allocator` 多了一层 holder，这层 holder 带来了什么好处又付出什么代价？

**答案**：好处是**鸭子类型 + SBO**——不要求资源继承基类（降低侵入），且 holder 内联在 `holder_` 字节数组里、无堆分配，`sizeof` 恒为两指针。代价是多一次**虚函数分派**（`get_holder().alloc` 经虚表转发到 `res_->allocate`），比 pmr 裸指针直接调 `allocate` 多一层间接；但 `allocate`/`deallocate` 本就要进系统堆或更重的策略，这点虚分派开销可忽略。

---

## 5. 综合实践

把本讲四块知识串起来，完成一个「**自定义资源 + 类型擦除 + 对齐验证**」的小任务。

**任务**：实现一个「带分配计数」的 memory resource，并用 `bytes_allocator` 持有它，分配若干不同对齐的内存块，验证三件事。

**步骤**：

1. 写一个资源类 `counting_resource`（示例代码，**非项目原有代码**），内部维护 `std::size_t count = 0` 与 `std::size_t max_align_req = 0`；`allocate(bytes, align)` 里 `++count`、`max_align_req = std::max(max_align_req, align)`，然后委托给 `ipc::mem::new_delete_resource::get()->allocate(bytes, align)`；`deallocate` 同样委托。
2. 用 `ipc::mem::bytes_allocator alc{&counting_resource_instance};` 持有它（注意：不继承任何基类即可）。
3. 通过 `alc` 分别 `allocate(100, 16)`、`allocate(200, 64)`、`allocate(300, 128)`，记录返回指针。
4. 验证三点：
   - **类型擦除**：`counting_resource.count` 应为 3，证明三次分配都经 `bytes_allocator` 转发到了你的资源。
   - **对齐生效**：每个返回指针地址 `% 对齐值 == 0`（因为最终走的是 `new_delete_resource` 的对齐分配）。
   - **字节级语义**：用 `alc.construct<T>(args...)` 构造一个对象（参考 [bytes_allocator.h:L150-L153](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L150-L153)），确认它会先按 `sizeof(T)`/`alignof(T)` 调用你的 `allocate`（计数再 +1）再做 placement new。
5. 析构前用 `alc.destroy(p)` 释放，确认计数对应的 `deallocate` 被调用。

**预期**：`counting_resource` 不继承任何东西却被 `bytes_allocator` 持有并转发；指针满足对齐；`construct` 触发按字节分配。这把「鸭子类型资格认定（4.1）→ 对齐分配落地（4.2）→ 类型擦除转发（4.3）→ 与 pmr 的差异（4.4）」四点全串起来了。具体运行结果**待本地验证**。

## 6. 本讲小结

- libipc 的内存子系统是三层架构：**资源接口约定**（鸭子类型）→ **资源实现**（`new_delete_resource` 等）→ **多态分配器**（`bytes_allocator` 持有资源）。
- 资格认定用 `has_allocate`/`has_deallocate`/`is_memory_resource` 三个 SFINAE trait 做**鸭子类型**判定（[bytes_allocator.h:L22-L47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L22-L47)），资源无需继承任何基类，只要方法签名对得上。
- `new_delete_resource` 用编译期 `#if` 在 `std::aligned_alloc`/`malloc`/`_aligned_malloc`/`posix_memalign` 间分派，并用 `round_up` 保证 C++17 分支的大小是对齐倍数（[new_delete_resource.cpp:L33-L64](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new_delete_resource.cpp#L33-L64)）。
- `bytes_allocator` 用 `holder_mr_base`/`holder_mr<MR,U>`/`holder_mr<MR,is_memory_resource<MR>>` 三层类族做**类型擦除**，且 holder **内联在 SBO 字节数组** `holder_` 里，故 `sizeof == 2 个指针`、零堆分配。
- 偏特化靠 `is_memory_resource` 是否成立来选择「转发」（合法资源）还是「空操作」（`void*` 等非资源）版本，这是鸭子类型落地为编译期分派的关键。
- 与 `std::pmr` 相比，libipc 不要求继承、按字节分配、SBO 持有；且 pmr 资源天然兼容 libipc，libipc 自定义资源却不兼容 pmr——libipc 约定是超集。

## 7. 下一步学习建议

本讲建立了分配器的**骨架与最底层资源**，接下来应该向上看「真实的分配策略」：

- **u7-l2 monotonic_buffer_resource 与中央 1MB 缓存**：`monotonic_buffer_resource` 用 bump 指针做单调分配、指数增长，并把一个 `bytes_allocator upstream_` 作为兜底委托（[memory_resource.h:L46-L80](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/memory_resource.h#L46-L80)）——你会看到 `bytes_allocator` 如何作为「上游」被组合，以及一个 memory resource 如何反过来被包进 `bytes_allocator` 持有。
- **u7-l3 block_pool 分层空闲链表 + `$new`/`$delete` + 容器分配器**：进一步看 `block_pool`/`central_cache_pool` 如何在 `bytes_allocator` 之上构建固定块空闲链表，以及类型擦除的析构器存储（`mem::$new`）如何复用本讲 `construct`/`destroy` 的思路（[new.h:L40-L105](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/new.h#L40-L105)）。

建议在进入 u7-l2 前，先把本讲的 `holder_mr` 类族继承关系和 SBO 尺寸推演手画一遍，确认你理解了「为何 `sizeof(bytes_allocator)` 恒为两指针」——这是后续所有策略复用 `bytes_allocator` 的共同地基。
