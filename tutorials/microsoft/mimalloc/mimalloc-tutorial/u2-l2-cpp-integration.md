# C++ 集成：new/delete 覆盖与 mi_stl_allocator

## 1. 本讲目标

上一讲（u2-l1）我们让 mimalloc 在**不改编译产物**的前提下接管了 C 程序的 `malloc`/`free`。本讲把战场搬到 C++，学完你应该能够：

1. 会用 `mimalloc-new-delete.h` 在**自己的源码里**全局替换 `new`/`delete`，并说清楚它为什么只能被包含进**一个**源文件。
2. 会用 `mi_stl_allocator` 让 `std::vector` 等容器的内存直接来自 mimalloc，而不动全局的 `new`/`delete`。
3. 区分 `mi_stl_allocator`、`mi_heap_stl_allocator`、`mi_heap_destroy_stl_allocator` 三者的语义差异与适用场景。
4. 理解 `mi_new` 与 `mi_malloc` 的关键差别：C++ 的内存耗尽（OOM）契约。

## 2. 前置知识

### 2.1 C++ 的 new/delete 其实是一个「重载家族」

很多初学者以为 `new` 就是一个运算符，实际上标准定义了**一组可替换的全局函数**。以本讲涉及的为例：

| 函数 | 引入标准 | 语义 |
| --- | --- | --- |
| `void* operator new(std::size_t)` | C++98 | 抛异常版：失败抛 `std::bad_alloc` |
| `void* operator new(std::size_t, const std::nothrow_t&)` | C++98 | 不抛版：失败返回 `nullptr` |
| `void operator delete(void*)` | C++98 | 普通 delete |
| `void operator delete(void*, std::size_t)` | C++14 | 带尺寸 delete（sized delete，便于分配器直接知道块大小） |
| `void* operator new(std::size_t, std::align_val_t)` | C++17 | 对齐 new（如 `new(std::align_val_t(32)) T`） |

每个都有对应的数组版 `new[]`/`delete[]`。**替换其中任何一个，对应的分配路径就会改走你的实现**——这就是 `mimalloc-new-delete.h` 的立足点。

### 2.2 C++ 的 OOM 契约：为什么不能直接用 mi_malloc

标准规定：抛异常版 `operator new` 在内存耗尽时，要先调用（若注册过的）`std::get_new_handler()` 指向的用户处理器尝试释放内存，仍失败才抛 `std::bad_alloc`。如果直接拿 `mi_malloc` 实现 `operator new`，失败时就只会返回 `nullptr`，违反契约。mimalloc 为此专门提供了 `mi_new` 家族——它们「实现 C++ 语义的 OOM 处理，而不是直接返回 NULL」，见后文 4.1.3。

### 2.3 STL 容器与 Allocator

`std::vector<int>` 其实是 `std::vector<int, std::allocator<int>>` 的缩写。第二个模板参数叫**分配器（allocator）**，容器所有内存申请/归还都经过它。标准只要求它提供 `allocate`、`deallocate` 等最小接口（C++17 起进一步精简）。**自定义一个满足该接口的类型，容器内存就换了一套房**——这就是 `mi_stl_allocator` 的立足点。

### 2.4 承接上一讲

u2-l1 讲过：mimalloc 的 override 构建（宏 `MI_MALLOC_OVERRIDE`）在**库内部**定义了与系统同名的 `malloc`/`free`，靠 ELF 符号抢占生效。本讲会看到一个重要事实：override 库**顺带也定义了 `operator new`/`operator delete`**，因此 Linux 下用 override 方式时根本不需要 `mimalloc-new-delete.h`——它主要服务于「链接普通版 mimalloc、又想接管 C++ 全局 new/delete」的场景（典型是 Windows）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/mimalloc-new-delete.h` | 67 行的小头文件：定义全局 `operator new`/`delete` 家族，转发给 `mi_new`/`mi_free` |
| `include/mimalloc.h` | 公共 API 头。本讲关注两段：`mi_new*` 函数族（约 L568-L579）与文件末尾的 STL 分配器模板（约 L585-L728） |
| `src/alloc-override.c` | override 构建时库内部对 new/delete 的定义（约 L232-L294），解释「为什么 Linux 下不用包含那个头」 |
| `test/main-override.cpp` | 官方 C++ 集成示例：`#ifdef _WIN32` 才包含 new-delete 头；并用六个测试函数示范三种 STL 分配器 |
| `test/CMakeLists.txt` | 官方示例工程：`dynamic-override-cxx` / `static-override-cxx` 两个目标演示 C++ 程序如何链接 mimalloc |
| `readme.md` | L282-L286：官方对 C++ 集成方式的推荐描述 |

## 4. 核心概念与源码讲解

### 4.1 用 mimalloc-new-delete.h 全局接管 new/delete

#### 4.1.1 概念说明

这个头文件做的事情极简单：**在全局命名空间定义标准 new/delete 家族的替换版本**，函数体一行——转发给 mimalloc。它的价值不在技术含量，而在「替你把 8+ 个重载全部写对」：

- 抛异常版、`nothrow` 版、数组版、sized 版、对齐版……漏写任何一个，漏写的路径仍走系统分配器，就会出现「同一进程两种分配器混管内存」的隐患。
- `operator new` 转发到 `mi_new`（而不是 `mi_malloc`），保住 C++ 的 OOM 契约。

readme 官方建议（对 C++ 程序为了最佳性能也应覆盖全局 new/delete，把这个头包含进**唯一一个**源文件）：[readme.md:282-286](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L282-L286)

#### 4.1.2 核心流程

一个 `Test* t = new Test(42);` 在包含该头之后的发生顺序：

```text
new Test(42)
  → 全局 operator new(sizeof(Test))        // 本头文件定义，见 4.1.3
      → mi_new(n)
          → 取当前线程默认 theap → 分配（快路径见后续单元）
          → 若失败：std::get_new_handler 流程 → 可能抛 std::bad_alloc
  → 在返回的内存上调用构造函数 Test(42)
...
delete t
  → 调用析构 ~Test()
  → 全局 operator delete(p)                 // 本头文件定义
      → mi_free(p) → 查 page map 反查所属页 → 入 free list
```

#### 4.1.3 源码精读

**(1) 文件头的三行警告。** 头文件开头的注释直接回答了本讲的核心问题：

[include/mimalloc-new-delete.h:11-20](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-new-delete.h#L11-L20)

> "This header should be included in only one source file!" 以及 "On Windows, or when linking dynamically with mimalloc, these can be more performant than the standard new-delete operations."

为什么只能一个源文件？因为这个头里写的是**函数定义**（有函数体的非 inline 全局函数），不是声明。C/C++ 的链接规则下，非 inline 的全局符号（这里的 `operator new` 等**不是** inline 函数）在每个包含它的翻译单元里都会生成一份强定义，两个以上源文件包含它，链接器必然报「multiple definition」。对比：`mi_stl_allocator` 是模板，天然允许多处包含（见 4.2）。

**(2) 只有 C++ 编译器才生效，并引入 MSVC SAL 标注。**

[include/mimalloc-new-delete.h:21-32](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-new-delete.h#L21-L32)

整个文件包在 `#if defined(__cplusplus)` 里；`mi_decl_new(n)` 宏在 MSVC 下展开出 `_Ret_notnull_`（返回值非空）、`_Post_writable_byte_size_(n)`（返回的 n 字节可写）这类 SAL 标注，让微软编译器的静态分析与 CRT 语义保持一致；其他编译器退化为 `mi_decl_nodiscard`（警告丢弃返回值）+ `mi_decl_restrict`（指针不别名，利于优化）。

**(3) delete 家族：普通版与 nothrow 版。**

[include/mimalloc-new-delete.h:34-38](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-new-delete.h#L34-L38)

`operator delete(void*)`、`operator delete[](void*)` 及两个 nothrow 版全部一行转发 `mi_free(p)`。nothrow 版的 `delete` 对应 nothrow 版的 `new`——配对使用是标准要求。

**(4) new 家族：普通版走 mi_new，nothrow 版走 mi_new_nothrow。**

[include/mimalloc-new-delete.h:40-44](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-new-delete.h#L40-L44)

注意 `noexcept(false)`：抛异常版的 `operator new` **允许**抛异常，这里显式标出（C++17 起标准如此要求）。`mi_new` 与 `mi_malloc` 的区别在 mimalloc.h 的注释里写得很清楚：

[include/mimalloc.h:568-576](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L568-L576)

> "The `mi_new` wrappers implement C++ semantics on out-of-memory instead of directly returning `NULL`. (and call `std::get_new_handler` and potentially raise a `std::bad_alloc` exception)."

**(5) sized delete（C++14 起）。**

[include/mimalloc-new-delete.h:46-49](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-new-delete.h#L46-L49)

带尺寸的 `operator delete(void* p, std::size_t n)` 转发 `mi_free_size(p, n)`——编译器在调用点已知对象大小，把尺寸一并传给分配器可用于校验。

**(6) 对齐 new/delete（C++17 起）。**

[include/mimalloc-new-delete.h:51-63](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-new-delete.h#L51-L63)

由 `__cpp_aligned_new` 特性宏守卫，覆盖 `align_val_t` 重载：分配走 `mi_new_aligned`，释放走 `mi_free_aligned` / `mi_free_size_aligned`。`static_cast<size_t>(al)` 把 `std::align_val_t` 转回普通整数传给 C 接口。

**(7) 为什么 Linux 下官方测试不包含这个头？** 看 `test/main-override.cpp` 的条件包含：

[test/main-override.cpp:17-24](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main-override.cpp#L17-L24)

只有 `_WIN32` 才包含。原因在 override 库自身：`src/alloc-override.c` 在 C++ 编译（或用 GCC/Clang 修饰名）时**已经在库里定义了整套 operator new/delete**：

[src/alloc-override.c:232-258](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L232-L258)

注释直言：「This is not really necessary as they usually call malloc/free anyway, but it improves performance」——即使 malloc 已被覆盖，CRT 的 new/delete 通常只是包了一层 malloc，直接替换成 `mi_new` 少一层转发。C 编译器下则用 Itanium ABI 修饰名定义（`_ZdlPv` 就是 `operator delete(void*)`）：[src/alloc-override.c:274-284](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L274-L284)

所以决策表是：

| 场景 | 是否需要包含 mimalloc-new-delete.h |
| --- | --- |
| Linux + `LD_PRELOAD` / 链接 override 版库 | 不需要（库里已有） |
| Windows + 覆盖版 DLL | 不需要（覆盖版已处理） |
| Windows / 任意平台 + 链接**普通版** mimalloc，想接管全局 new/delete | **需要**，包含进唯一一个 .cpp |
| 只想让个别容器走 mimalloc | 不需要，用 4.2 的 `mi_stl_allocator` |

#### 4.1.4 代码实践

**实践目标**：验证「包含 mimalloc-new-delete.h 后，全局 `new`/`delete` 确实改走 mimalloc」。

**操作步骤**：

1. 参照 `test/CMakeLists.txt` 中官方 C++ 目标的做法（[test/CMakeLists.txt:28-29](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt#L28-L29) 的 `dynamic-override-cxx`：`add_executable(... main-override.cpp)` + `target_link_libraries(... mimalloc)`），写一个单文件程序 `main.cpp`（示例代码，非仓库原有）：

   ```cpp
   // main.cpp —— 全项目唯一包含该头的源文件
   #include <mimalloc-new-delete.h>
   #include <cstdio>

   class Widget {
     char payload[80];
   public:
     Widget() { std::printf("Widget constructed\n"); }
     ~Widget() { std::printf("Widget destroyed\n"); }
   };

   int main() {
     Widget* w = new Widget();          // 走 mi_new
     Widget* w2 = new (std::nothrow) Widget();  // 走 mi_new_nothrow
     delete w;                          // 走 mi_free
     delete w2;
     char* buf = new char[1024];        // 走 operator new[] -> mi_new
     delete[] buf;                      // 走 operator delete[] -> mi_free
     return 0;
   }
   ```

2. 编译链接（路径按你的安装位置调整，示例命令）：

   ```bash
   g++ -std=c++17 -O2 main.cpp -I<安装目录>/include/mimalloc -L<安装目录>/lib -lmimalloc -o demo
   ```

3. 用**debug 构建**（统计默认开启，承接 u1-l4 的结论：release 构建不打印 blocks 段）运行：

   ```bash
   MIMALLOC_SHOW_STATS=1 ./demo
   ```

**需要观察的现象**：退出时的统计报表里，`80` 字节与 `1024` 字节对应的 bin 行 `total#`（累计分配次数）各增加了 1~2；`not all freed` 不应出现。

**预期结果**：new/delete 的每次分配都体现在 mimalloc 统计中，证明转发链 `operator new → mi_new → theap` 生效。具体数值「待本地验证」（构造/析构打印与统计行内容依赖你的构建类型与平台）。

#### 4.1.5 小练习与答案

**练习 1**：把 `mimalloc-new-delete.h` 同时包含进 `a.cpp` 和 `b.cpp` 一起链接，会发生什么？为什么 `mi_stl_allocator` 就没这个问题？

**答案**：链接器报「multiple definition of `operator new(unsigned long)`」之类的错误。因为该头给出的是非 inline 的全局函数**定义**，每个翻译单元各生成一份强符号；而 `mi_stl_allocator` 是类模板，模板实例化遵守 ODR（单一定义规则）的模板豁免，多处实例化同一份定义是合法的。

**练习 2**：`operator new` 的实现为什么调用 `mi_new(n)` 而不是更直接的 `mi_malloc(n)`？

**答案**：标准抛异常版 `operator new` 的 OOM 契约是：先执行已注册的 `new_handler`，最终仍失败则抛 `std::bad_alloc`。`mi_malloc` 失败只会返回 `NULL`；而 `mi_new` 家族按 mimalloc.h L568-L569 的注释实现了完整的 C++ OOM 语义。

**练习 3**：`new (std::nothrow) T` 和 `new T` 在本讲头文件里分别落到哪两个 mimalloc 函数？

**答案**：`mi_new_nothrow(n)`（[include/mimalloc-new-delete.h:43-44](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-new-delete.h#L43-L44)）与 `mi_new(n)`（[include/mimalloc-new-delete.h:40-41](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-new-delete.h#L40-L41)）。

### 4.2 mi_stl_allocator：无状态 STL 分配器

#### 4.2.1 概念说明

如果不想动全局 new/delete，只想让**某些容器**用 mimalloc，就把容器的分配器模板参数换成 `mi_stl_allocator<T>`：

```cpp
std::vector<int, mi_stl_allocator<int>> vec;
```

它是**无状态（stateless）**分配器：自己不持有任何数据，`allocate` 直接调 `mi_new_n`（默认 theap），`deallocate` 直接调 `mi_free`。任何两个 `mi_stl_allocator` 实例都视为相等——内存来自同一个进程级分配器，可以随意互相释放。

#### 4.2.2 核心流程

```text
vector 扩容需要 count 个 T
  → allocator.allocate(count)
      → mi_new_n(count, sizeof(T))       // 带乘法溢出检查的 count*size + C++ OOM 语义
  → 拷贝/移动旧元素 → allocator.deallocate(旧指针, 旧count)
      → mi_free(p)
```

`mi_new_n` 相比 `mi_malloc(n * size)` 的额外价值：`count * size` 的溢出被单独检查（`mi_attr_alloc_size2(1,2)` 标注两个参数），并把 OOM 处理为 `std::bad_alloc`。

#### 4.2.3 源码精读

**(1) 公共基类 `_mi_stl_allocator_common<T>`**：给出所有分配器共享的typedef 与 C++11 起的 `propagate_on_container_*` 三件套（容器拷贝/移动/交换时分配器跟着走）、`construct`/`destroy`（placement new 与显式析构）：

[include/mimalloc.h:598-621](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L598-L621)

**(2) `mi_stl_allocator<T>` 本体**：核心就两个函数。

[include/mimalloc.h:623-645](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L623-L645)

- `allocate(count)` → `mi_new_n(count, sizeof(T))`（C++17 与之前版本的重载形式略不同，见 L635-L640 的条件编译）；
- `deallocate(p, n)` → `mi_free(p)`，尺寸参数被忽略——mimalloc 靠页元数据自识别块大小（下一单元讲 page map 时你会看到它如何从任意指针反查出尺寸）；
- `is_always_equal = std::true_type`（L643-L644）：向标准宣告「无状态、任意实例等价」；
- `rebind` 模板（L627）：让 `std::list<T>` 之类需要内部分配链表节点的容器能把分配器「重绑定」到节点类型上。

**(3) 相等比较**：所有实例恒等。

[include/mimalloc.h:647-648](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L647-L648)

**官方用法示范**见 [test/main-override.cpp:155-160](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main-override.cpp#L155-L160)：`std::vector<int, mi_stl_allocator<int> >` 直接 `push_back`/`pop_back`。

#### 4.2.4 代码实践

**实践目标**：验证 `mi_stl_allocator` 路径的分配确实进入 mimalloc 统计（这就是本讲规格指定的主实践）。

**操作步骤**：

1. 写 `main.cpp`（示例代码）。注意：本例同时演示两种集成方式的**分工**——全局 new/delete 由头文件接管，vector 走 `mi_stl_allocator`：

   ```cpp
   #include <mimalloc-new-delete.h>   // 唯一包含点：接管全局 new/delete
   #include <mimalloc.h>
   #include <vector>
   #include <cstdio>

   int main() {
     // 一百万个 int，走 mi_stl_allocator -> mi_new_n
     std::vector<int, mi_stl_allocator<int>> vec;
     vec.reserve(1000 * 1000);
     for (int i = 0; i < 1000 * 1000; ++i) vec.push_back(i);

     // 对照组：默认分配器的 vector，走被接管的 operator new
     std::vector<int> plain;
     plain.reserve(1000 * 1000);
     for (int i = 0; i < 1000 * 1000; ++i) plain.push_back(i);

     std::printf("sum=%d %d\n", vec.back(), plain.back());
     return 0;   // 两个 vector 析构，内存归还
   }
   ```

   由于 `reserve` 一次到位，`vec` 的数据区是一次 4,000,000 字节的分配（一百万 int × 4 字节），`plain` 同理。

2. debug 构建、带统计运行：

   ```bash
   MIMALLOC_SHOW_STATS=1 ./demo
   ```

**需要观察的现象**：统计中约 3.8MiB 量级的分配恰好出现**两次**（两个 vector 各一次）——`mi_stl_allocator` 的那次直接计入其调用线程 theap 的统计，`plain` 的那次经由 `operator new → mi_new` 同样计入。两条路径殊途同归于 mimalloc。

**预期结果**：两个 4MiB 左右的块在 bin/large 统计行可见，退出时归零、无泄漏告警。具体落在哪一档（medium/large bin 边界按 `MI_SMALL_MAX_OBJ_SIZE` 等宏划分）「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`mi_stl_allocator<int>::deallocate(p, n)` 忽略了 `n`，mimalloc 靠什么知道该释放多大？

**答案**：靠分配器内部元数据。mimalloc 从指针出发经 page map 找到所属页，页记录着本页所有块的 size class，因此 `mi_free(p)` 无需尺寸参数（这正是 malloc 风格接口的底层前提；sized 版 `mi_free_size` 只是多做一层一致性校验）。

**练习 2**：为什么 `mi_stl_allocator` 敢把 `is_always_equal` 设为 `true_type`，而后面 4.3 的堆版却是 `false_type`？

**答案**：`mi_stl_allocator` 无状态，所有实例的内存都出自同一个进程级分配器，任意实例可互相释放；堆版分配器各自持有不同的 `mi_heap_t*`，来自不同堆的分配器实例不等价，必须逐个比较持有的堆。

**练习 3**：`std::list<int, mi_stl_allocator<int>>` 里的链表节点不是 `int`，分配器怎么分配节点？

**答案**：通过 `rebind` 模板（[include/mimalloc.h:627](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L627)），容器把 `mi_stl_allocator<int>` 重绑定为 `mi_stl_allocator<_List_node<int>>` 再分配节点。

### 4.3 mi_heap_stl_allocator 与 mi_heap_destroy_stl_allocator：容器级一等堆

#### 4.3.1 概念说明

第三种集成方式面向 v3 的**一等堆（first-class heap）**：让一个容器的全部内存落在专属的 `mi_heap_t` 里，实现「整堆一次性释放」或「与其他数据隔离」。mimalloc.h 提供：

- `mi_heap_stl_allocator<T>`：默认构造时**自建新堆**，最后一个引用释放时调用 `mi_heap_delete` 整堆回收；也可用现成堆指针构造（不接管所有权）。
- `mi_heap_destroy_stl_allocator<T>`：激进场——`deallocate` **什么都不做**，等堆析构时 `mi_heap_destroy` 一次性放掉全部内存。源码注释警告 "use with care!"（[include/mimalloc.h:710-711](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L710-L711)）。

两者都要求 C++11 以上（由 `MI_HAS_HEAP_STL_ALLOCATOR` 宏标记，[include/mimalloc.h:651-652](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L651-L652)）。

#### 4.3.2 核心流程

堆版分配器的生命周期由 `std::shared_ptr<mi_heap_t>` 管理引用计数：

```text
构造（无参）
  → mi_heap_new() 创建新堆
  → shared_ptr 以 heap_delete（或 heap_destroy）作删除器封装
容器拷贝/扩容 rebind
  → 拷贝构造 shared_ptr —— 引用计数 +1，多个分配器实例共享同一堆
allocate(count)
  → mi_heap_alloc_new_n(heap, count, sizeof(T))   // 落在指定堆
最后一个 shared_ptr 释放
  → 删除器执行：mi_heap_delete(堆)（普通版）
                或 mi_heap_destroy(堆)（destroy 版，整堆内存一次归还）
```

#### 4.3.3 源码精读

**(1) 公共基类 `_mi_heap_stl_allocator_common<T, _mi_destroy>`**：模板第二参数 `_mi_destroy` 决定堆回收方式。

[include/mimalloc.h:657-692](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L657-L692)

关键点：

- L662：用现成堆构造时，删除器是空 lambda `[](mi_heap_t*) {}`——**不删不销毁**传入的堆（所有权仍在调用者）；
- L679：堆由 `std::shared_ptr<mi_heap_t>` 持有，引用计数天然解决「容器拷贝后多个分配器实例共享堆」的问题；
- L682-L685：默认构造 `mi_heap_new()` 建新堆，`_mi_destroy` 为真用 `heap_destroy`、为假用 `heap_delete` 作删除器；
- L690-L691：两个静态删除器分别包 `mi_heap_delete` / `mi_heap_destroy`（后者不运行析构、直接整堆回收）；
- L672：`is_always_equal = std::false_type`——有状态分配器，相等性要看堆（L676 的 `is_equal` 比较堆指针）；
- L675：`collect(force)` 便捷方法转发 `mi_heap_collect`。

**(2) `mi_heap_stl_allocator<T>`（`_mi_destroy=false`）**：

[include/mimalloc.h:695-704](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L695-L704)

分配走 `mi_heap_alloc_new_n`（基类 L665-L668），释放走 `mi_free(p)`（L702）——注意释放仍用全局 `mi_free`，mimalloc 能从指针反查出它属于哪个堆，无需归还到「正确」的堆对象。

**(3) `mi_heap_destroy_stl_allocator<T>`（`_mi_destroy=true`）**：

[include/mimalloc.h:710-721](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L710-L721)

最大差异在 L719：`deallocate(T*, size_type)` **空实现**——逐个释放被刻意跳过，攒到最后 `mi_heap_destroy` 一锅端。代价是：destroy 版堆里的对象析构函数不会被批量调用语义所覆盖（`mi_heap_destroy` 语义是直接回收内存），所以容器元素必须是不持有外部资源的类型（如 `int`、POD），这就是 "use with care" 的含义。

**(4) 三者对比一览**：

| | `mi_stl_allocator` | `mi_heap_stl_allocator` | `mi_heap_destroy_stl_allocator` |
| --- | --- | --- | --- |
| 状态 | 无 | `shared_ptr<mi_heap_t>` | 同左 |
| allocate | `mi_new_n`（默认堆） | `mi_heap_alloc_new_n(自有堆)` | 同左 |
| deallocate | `mi_free` | `mi_free` | **空操作** |
| 堆回收 | 无（各自进默认堆） | 最后引用 `mi_heap_delete` | 最后引用 `mi_heap_destroy`（整堆一次） |
| 实例相等 | 恒等 | 同堆才等 | 同堆才等 |
| 适用 | 一般加速 | 一组容器共享独立堆、可单独 `collect` | 短命大容器、元素无外部资源、想省掉逐块 free |

官方用法示范：[test/main-override.cpp:183-211](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main-override.cpp#L183-L211) 依次用四个 vector 测 `mi_heap_stl_allocator<int>/<some_struct>` 与 `mi_heap_destroy_stl_allocator<int>/<some_struct>`，由 `#if MI_HAS_HEAP_STL_ALLOCATOR` 守卫。

#### 4.3.4 代码实践

**实践目标**：直观对比 `mi_heap_stl_allocator` 与 `mi_heap_destroy_stl_allocator` 的释放行为差异。

**操作步骤**：

1. 写对照小程序（示例代码）：

   ```cpp
   #include <mimalloc.h>
   #include <vector>
   #include <cstdio>

   int main() {
     { // A：普通堆版，逐块释放
       std::vector<int, mi_heap_stl_allocator<int>> va;
       for (int i = 0; i < 100000; ++i) va.push_back(i);
       std::printf("A size=%zu\n", va.size());
     } // va 析构：逐块 mi_free + 最后引用 mi_heap_delete
     { // B：destroy 版，逐块释放被跳过
       std::vector<int, mi_heap_destroy_stl_allocator<int>> vb;
       for (int i = 0; i < 100000; ++i) vb.push_back(i);
       std::printf("B size=%zu\n", vb.size());
     } // vb 析构：deallocate 空转 + 最后引用 mi_heap_destroy 整堆回收
     return 0;
   }
   ```

2. 用 debug 构建，在两段作用域结束处各打印一次进程 RSS（或用 `mi_stats_print(NULL)` 分段输出）。

**需要观察的现象**：A、B 都能正确归还内存（destroy 版靠整堆 destroy 而非逐块 free）；若在两个作用域间插入统计输出，B 段的 `freed` 计数几乎为零而堆回收计数增加。

**预期结果**：两个 vector 功能等价、最终内存都归零，但内部路径一个走 `mi_free` ×N，一个走 `mi_heap_destroy` ×1。精确计数「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`mi_heap_stl_allocator<int> a;`（默认构造）与 `mi_heap_stl_allocator<int> b(mi_heap_new());` 有何本质区别？

**答案**：`a` 自建新堆且 `shared_ptr` 持有所有权，引用归零时自动 `mi_heap_delete`；`b` 包裹外部堆，删除器为空 lambda（[include/mimalloc.h:662](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L662)），堆的生死由外部代码负责，分配器只「借用」。

**练习 2**：为什么 `mi_heap_stl_allocator` 的 `deallocate` 可以直接 `mi_free(p)` 而不需要拿到堆指针？

**答案**：`mi_free` 经 page map 从指针反查所属页、进而知道所属堆，释放天然路由回正确的堆；分配时才必须显式指定堆（决定放进哪个堆的页队列）。

**练习 3**：`mi_heap_destroy_stl_allocator<std::string>` 是危险用法吗？为什么？

**答案**：危险。`mi_heap_destroy` 直接整堆回收内存，不会为每个元素执行析构；`std::string` 可能持有堆外资源（SSO 之外的内嵌指针指向的字符缓冲恰好同堆时尚可，但若字符串持有其他外部资源则必然泄漏）。该分配器只适合元素类型「无外部资源、无需析构副作用」的场景（如 `int`、POD），与源码注释 "use with care" 一致。

## 5. 综合实践

把本讲三种集成方式串成一个「同台对比」小工具（示例代码）：

```cpp
// bench.cpp —— 全项目唯一包含 new-delete 头的源文件
#include <mimalloc-new-delete.h>
#include <mimalloc.h>
#include <vector>
#include <chrono>
#include <cstdio>

template<class Alloc>
long long fill(int n) {
  std::vector<int, Alloc> v;
  v.reserve(n);
  for (int i = 0; i < n; ++i) v.push_back(i);
  return v.back();
}

int main() {
  const int N = 5 * 1000 * 1000;
  using clk = std::chrono::steady_clock;

  auto t0 = clk::now();  long long s1 = fill<std::allocator<int>>(N);        // 全局 new/delete（已被头接管）
  auto t1 = clk::now();  long long s2 = fill<mi_stl_allocator<int>>(N);      // 无状态 STL 分配器
  auto t2 = clk::now();  long long s3 = fill<mi_heap_destroy_stl_allocator<int>>(N); // 整堆一次性回收
  auto t3 = clk::now();

  auto ms = [](auto a, auto b){ return std::chrono::duration<double,std::milli>(b-a).count(); };
  std::printf("default(new/delete)  : %8.2f ms, sum=%lld\n", ms(t0,t1), s1);
  std::printf("mi_stl_allocator     : %8.2f ms, sum=%lld\n", ms(t1,t2), s2);
  std::printf("heap_destroy_stl     : %8.2f ms, sum=%lld\n", ms(t2,t3), s3);
  mi_stats_print(NULL);
  return 0;
}
```

要求完成：

1. 用 release 构建（计时）与 debug 构建（看统计）各跑一遍；
2. 解释三段耗时差异中「分配」与「释放」各自占比的原因（提示：destroy 版省掉 N 次逐块 free，但 vector 扩容策略三者相同）；
3. 对照 `test/main-override.cpp` 的官方测试函数（L155-L222），把你的三个 `fill` 与 `test_stl_allocator1/3/5` 一一对应起来。

预期：三条路径都能正确工作；具体耗时排序「待本地验证」（受编译器、优化级别与平台影响）。

## 6. 本讲小结

- `mimalloc-new-delete.h` 用约 30 行转发代码把 C++ 的 new/delete 全家族（普通/nothrow/数组/sized/对齐）接到 mimalloc 上；它给出的是非 inline 全局函数**定义**，因此**只能包含进一个源文件**，否则链接期多重定义。
- `operator new` 转发到 `mi_new` 而非 `mi_malloc`：前者实现 C++ OOM 契约（`std::get_new_handler` → `std::bad_alloc`），这是「看起来只是换个函数」里最容易踩的语义坑。
- Linux 下链接 override 版库时不需要这个头——`src/alloc-override.c` 已在库内定义了 new/delete（还顺带解释了 `test/main-override.cpp` 为何只在 `_WIN32` 包含它）；该头主要服务于链接普通版库又要接管 C++ 全局分配的场景。
- `mi_stl_allocator` 是无状态分配器：`allocate → mi_new_n`、`deallocate → mi_free`、`is_always_equal = true_type`，适合「只加速个别容器」。
- `mi_heap_stl_allocator` / `mi_heap_destroy_stl_allocator` 用 `shared_ptr<mi_heap_t>` 持有一等堆：前者逐块释放、最后引用 `mi_heap_delete`；后者 `deallocate` 空转、最后引用 `mi_heap_destroy` 整堆一锅端，只适合无外部资源的元素类型。

## 7. 下一步学习建议

本讲之后，你已经会「用」mimalloc 的全部主流接入方式（C API、malloc 覆盖、C++ 覆盖、STL 分配器）。下一讲 u2-l3 转向**选项系统**：`MIMALLOC_*` 环境变量与 `mi_option_set` 编程接口如何控制 purge_delay 等行为。之后进入单元三，开始拆数据结构——建议优先阅读 `include/mimalloc/types.h` 中 `mi_heap_t` 的定义，因为本讲的 `shared_ptr<mi_heap_t>` 只是堆的「引用外壳」，堆里面到底装了什么（页队列、统计、tld），是下一阶段的第一课。
