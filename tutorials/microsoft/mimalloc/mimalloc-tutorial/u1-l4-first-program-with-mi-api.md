# 第一次使用 mi_ API：从 mi_malloc 到 MIMALLOC_SHOW_STATS

## 1. 本讲目标

学完本讲，你应该能够：

1. 正确包含 `mimalloc.h` 并把程序链接到 `libmimalloc`（静态库或动态库）。
2. 使用 `mi_malloc` / `mi_zalloc` / `mi_calloc` / `mi_strdup` / `mi_free` 完成一次完整的「分配 → 使用 → 释放」周期，并说清楚这四个分配函数在语义上的差别。
3. 用 `MIMALLOC_SHOW_STATS=1`（或 `mi_option_enable(mi_option_show_stats)`）让程序退出时打印分配统计报表，并能逐列解读输出中的 `bin` 行、`pages` 段和 `process` 段。

本讲不深入分配器内部实现（那是单元四、单元五的事），只解决一个问题：**把 mimalloc 当成一个普通的 C 库用起来，并学会看它的「体检报告」**。

## 2. 前置知识

阅读本讲前，你应当已经完成 u1-l2 的构建实践（知道 `out/release/`、`out/debug/` 下有哪些库文件）。此外需要以下基础概念：

- **malloc 家族语义**：`malloc(n)` 分配 n 字节（内容未定义）；`calloc(count, size)` 分配 count×size 字节并**清零**；`realloc` 调整大小；`free` 释放。`mi_` 前缀的函数与它们一一对应，语义相同。
- **size class 与 bin**：分配器不会为「任意字节数」单独管理内存，而是把请求尺寸归入有限的若干「尺寸类别」（size class）。统计报表里的 `bin 12` 就表示第 12 号尺寸类别。这个概念在 u1-l1 已提过，本讲只需知道「一个 bin = 一种块的规格」。
- **环境变量**：mimalloc 的所有运行时开关都可以用 `MIMALLOC_` 前缀的环境变量设置，例如 `MIMALLOC_SHOW_STATS=1`。它不需要改代码，是观察分配器行为的第一工具。
- **默认堆（default theap）**：mimalloc 为每个线程准备一个「线程本地堆」（theap）。调用 `mi_malloc` 时不需要传堆参数——它自动使用当前线程的默认 theap。本讲只用这个默认堆；一等堆（`mi_heap_new` 等）留到 u7-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/mimalloc.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h) | **整个库唯一的公共 API 头文件**。所有 `mi_` 函数声明、选项枚举、C++ STL 适配器都在这里。本讲主角之一。 |
| [test/test-api.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c) | 官方 API 表面测试：几百个小用例演示每个 `mi_` 函数的正确用法与边界行为。本讲另一主角。 |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) | `mi_malloc` 等入口的实现（本讲只看入口几行，不深入）。 |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | `mi_free` / `mi_usable_size` 的实现。 |
| [src/options.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c) | 选项系统：环境变量 `MIMALLOC_*` 如何变成内部选项值。 |
| [src/stats.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c) | 统计的收集与打印：`bin` 行的格式就定义在这里。 |
| [src/init.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c) | 进程退出流程：`MIMALLOC_SHOW_STATS` 在这里触发报表打印。 |
| [test/testhelper.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/testhelper.h) | 测试用的 `CHECK` / `CHECK_BODY` 宏，理解它才能读懂 test-api.c。 |

## 4. 核心概念与源码讲解

本讲的最小模块有两个：**include/mimalloc.h（公共 API）** 与 **test/test-api.c（用法示范）**，下面再拆出两个子模块分别讲「分配周期的实现入口」和「统计输出的生成与解读」。

### 4.1 include/mimalloc.h：整个库唯一的公共入口

#### 4.1.1 概念说明

mimalloc 对外只暴露一个头文件 `mimalloc.h`。它没有依赖任何其他头（只包含 `<stddef.h>` 和 `<stdbool.h>`），因此可以安全地放进任何 C/C++ 项目。文件开头第 11 行的版本宏告诉我们当前 API 版本：

- [include/mimalloc.h:L11](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L11) —— `MI_MALLOC_VERSION 30500`，即 v3.5.0（主版本 + 两位次版本 + 两位补丁版本）。程序里可用 `mi_version()` 在运行时取回它。

这个头文件按「功能分区」组织，分区顺序恰好也是学习顺序：

| 分区 | 行号范围（约） | 内容 |
| --- | --- | --- |
| 标准 malloc 接口 | L105-L117 | `mi_malloc` / `mi_calloc` / `mi_realloc` / `mi_free` / `mi_strdup` 等 |
| 扩展分配 | L119-L141 | `mi_zalloc` / `mi_malloc_small` / `mi_usable_size` / `mi_good_size` |
| 对齐分配 | L143-L156 | `mi_malloc_aligned` / `mi_malloc_aligned_at` 等 |
| 类型化分配宏 | L159-L176 | `mi_malloc_tp(int)` 等 |
| 回调与进程信息 | L179-L201 | `mi_register_output` / `mi_process_info` |
| 一等堆 | L229-L294 | `mi_heap_new` / `mi_heap_malloc` 等（u7-l3 详讲） |
| 选项 | L459-L533 | `mi_option_t` 枚举与 `mi_option_get/set` |
| POSIX/C++ 适配 | L536-L728 | `mi_posix_memalign`、`mi_stl_allocator` 等 |

#### 4.1.2 核心流程

使用 mimalloc 的最小程序只有三步：

```text
1. #include "mimalloc.h"          （安装后则是 <mimalloc.h>）
2. 调用 mi_malloc / mi_zalloc / mi_calloc / mi_strdup 分配
3. 用 mi_free 释放（同一个指针，且只能释放一次）
```

编译链接时把库路径指到构建产物即可（产物名见 u1-l2：release 下是 `libmimalloc.so` / `libmimalloc.a`，非 release 构建库名会带 `-debug` 等后缀）。

#### 4.1.3 源码精读

标准 malloc 接口的声明（注意每个函数都带 `mi_attr_noexcept` 和分配器属性标注）：

- [include/mimalloc.h:L109-L117](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L109-L117) —— 声明 `mi_malloc`、`mi_calloc`、`mi_realloc`、`mi_expand`、`mi_free`、`mi_strdup`、`mi_strndup`、`mi_realpath`。`mi_decl_nodiscard` 提醒你必须检查返回值，`mi_attr_malloc` 告诉编译器返回的指针不与任何存活指针重叠（利于优化）。

扩展分配里最常用的是「清零分配」和「查询函数」：

- [include/mimalloc.h:L122-L134](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L122-L134) —— 定义 `MI_SMALL_SIZE_MAX`（小对象上限，128 字 × 指针宽度，64 位下 1KiB），并声明 `mi_zalloc`（清零版 `mi_malloc`）、`mi_usable_size`（查询指针实际可用的字节数）、`mi_good_size`（问「我要 n 字节，实际会给我多大的块」）。

类型化宏让 C 代码少写一次强转：

- [include/mimalloc.h:L163-L169](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L163-L169) —— `mi_malloc_tp(tp)` 展开为 `((tp*)mi_malloc_csize(sizeof(tp)))`，`mi_free_tp(tp,p)` 对应释放。`mi_malloc_csize` 是 L398 的 `static inline` 辅助函数：小对象走更快的 `mi_malloc_small`。

选项枚举与编程接口（本讲 4.4 会用到 `mi_option_show_stats`）：

- [include/mimalloc.h:L463-L520](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L463-L520) —— `mi_option_t` 枚举。前三个是稳定选项：`mi_option_show_errors`、`mi_option_show_stats`、`mi_option_verbose`；其余为高级选项，注释里标了默认值（如 `mi_option_purge_delay` 默认 10 毫秒）。
- [include/mimalloc.h:L523-L533](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L523-L533) —— `mi_option_is_enabled` / `mi_option_enable` / `mi_option_get` / `mi_option_set` 等编程接口，效果与环境变量等价。

#### 4.1.4 代码实践

**实践目标**：验证「包含头文件 + 链接库」这条最小路径可行。

1. 按 u1-l2 完成 release 构建，假设产物在 `out/release/`。
2. 新建 `hello-mi.c`（示例代码，非项目原有文件）：

   ```c
   #include <stdio.h>
   #include <mimalloc.h>

   int main(void) {
     char* s = mi_strdup("hello mimalloc");
     printf("%s (usable: %zu bytes)\n", s, mi_usable_size(s));
     mi_free(s);
     return 0;
   }
   ```

3. 编译运行（Linux，其余平台见 u1-l2 的产物说明）：

   ```bash
   gcc -I<仓库路径>/include -o hello-mi hello-mi.c -L<仓库路径>/out/release -lmimalloc
   LD_LIBRARY_PATH=<仓库路径>/out/release ./hello-mi
   ```

4. **需要观察的现象**：程序打印 `hello mimalloc (usable: N bytes)`，N 是 `mi_strdup` 分配的块按 size class 取整后的可用字节数（14 字符 + 结尾 NUL 共 14 字节，N 会略大于 14，具体数值与平台指针宽度有关，待本地验证）。
5. **预期结果**：编译无未定义符号错误、运行正常输出。若链接报错找不到 `mi_malloc`，先确认 `libmimalloc.so` 的实际文件名是否带构建类型后缀。

#### 4.1.5 小练习与答案

**练习 1**：`mi_malloc(0)` 合法吗？返回什么？
**答案**：合法。test-api.c 中专门有用例（见 4.3.3），断言返回非 NULL 指针，随后正常 `mi_free`。这是 mimalloc 有意与 C 标准保持一致的行为。

**练习 2**：`mi_usable_size` 与 `mi_good_size` 有什么区别？
**答案**：`mi_usable_size(p)` 的入参是**已分配的指针**，返回这个块实际可用的字节数（通常 ≥ 请求值，因为按 size class 取整）；`mi_good_size(n)` 的入参是一个**尺寸**，返回「如果现在请求 n 字节会拿到多大的块」，不需要真的分配。

**练习 3**：为什么 `mimalloc.h` 里很多声明带有 `mi_decl_nodiscard`？
**答案**：它展开为 C++17 的 `[[nodiscard]]` 或 GCC/Clang 的 `warn_unused_result`（见 [include/mimalloc.h:L27-L37](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L27-L37)）。分配函数返回 NULL 表示内存不足，丢弃返回值几乎必然是 bug，所以让编译器直接告警。

### 4.2 一次完整的分配周期：从 mi_malloc 到 mi_free

#### 4.2.1 概念说明

四个常用分配函数的差别只在一件事上——**是否清零、是否算个数**：

| 函数 | 请求方式 | 内容初值 | 对应标准函数 |
| --- | --- | --- | --- |
| `mi_malloc(n)` | n 字节 | 未定义 | `malloc` |
| `mi_zalloc(n)` | n 字节 | **全零** | （无直接对应，等价 `calloc(1,n)`） |
| `mi_calloc(count, size)` | count × size 字节 | **全零**，且乘法溢出时返回 NULL | `calloc` |
| `mi_strdup(s)` | strlen(s)+1 字节并复制字符串 | 字符串内容 | `strdup`（POSIX） |

释放统一用 `mi_free`——不管块来自哪个 `mi_` 分配函数。这比某些分配器的「成对使用」规则简单得多。

#### 4.2.2 核心流程

一次分配周期在源码层面的路径（只看入口，内部机制后续单元展开）：

```text
mi_malloc(size)
  └─ mi_theap_malloc(_mi_theap_default(), size)   // 取当前线程默认 theap，进入通用入口
       └─ （快路径：从 theap 当前页的 free list 弹出一个块 → 单元四精读）

mi_free(p)
  └─ mi_validate_ptr_page_nonnull(p, ...)          // 通过 page map 反查 p 所属的页
       └─ （本线程释放：把块挂回页的 free list → 单元五精读）
```

关键认知：**所有不带 `heap`/`theap` 的 `mi_` 函数，第一步都是取默认 theap**。这就是「无锁快路径」的第一块拼图——每线程一个堆，互不干扰。

#### 4.2.3 源码精读

- [src/alloc.c:L256-L258](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L256-L258) —— `mi_malloc` 的全部本体：一行，把请求转给「默认 theap 上的分配」。复杂度都被推到了内层函数。
- [src/alloc.c:L283-L285](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L283-L285) —— `mi_zalloc`：与 `mi_malloc` 唯一区别是第三个参数 `true`（zero 标志），由内层在弹出块时顺手清零。
- [src/alloc.c:L291-L299](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L291-L299) —— `mi_calloc` 的防溢出要点：先用 `mi_count_size_overflow(count, size, &total)` 检查 count×size 是否溢出，溢出直接返回 NULL（这正是 test-api.c 中 `calloc-overflow` 用例验证的行为），否则等价于 `mi_zalloc(total)`。
- [src/alloc.c:L551-L553](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L551-L553) —— `mi_strdup`：转给 `mi_theap_strdup`，后者用 `mi_theap_malloc(len+1)` 分配再复制（见 [src/alloc.c:L543-L549](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L543-L549)），结尾补 `\0`。
- [src/free.c:L251-L253](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L251-L253) —— `mi_free` 入口：先 `mi_validate_ptr_page_nonnull(p, "mi_free", ...` 把指针反查成所属页（page map 的作用，u3-l4 详讲），成功才继续释放。
- [src/free.c:L310-L312](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L310-L312) —— `mi_free_size(p, size)`：带尺寸的释放，debug 构建下会顺便校验尺寸是否匹配，某些路径比通用 `mi_free` 略快。

#### 4.2.4 代码实践

**实践目标**：亲手完成本讲规格要求的「四函数分配周期」小程序，并验证清零语义。

1. 新建 `cycle-mi.c`（示例代码）：

   ```c
   #include <stdio.h>
   #include <stdint.h>
   #include <string.h>
   #include <mimalloc.h>

   static int is_zero(const uint8_t* p, size_t n) {
     for (size_t i = 0; i < n; i++) if (p[i]) return 0;
     return 1;
   }

   int main(void) {
     uint8_t* a = mi_malloc(100);              // 未定义内容
     uint8_t* b = mi_zalloc(100);              // 应全零
     uint8_t* c = mi_calloc(25, 4);            // 25*4=100 字节，应全零
     char*     d = mi_strdup("mimalloc");      // 9 字节
     printf("zalloc zero? %d, calloc zero? %d\n", is_zero(b,100), is_zero(c,100));
     printf("usable: malloc=%zu zalloc=%zu calloc=%zu strdup=%zu\n",
            mi_usable_size(a), mi_usable_size(b), mi_usable_size(c), mi_usable_size(d));
     mi_free(a); mi_free(b); mi_free(c); mi_free(d);
     return 0;
   }
   ```

2. 用与 4.1.4 相同的方式编译运行（也可直接放进 `test/` 目录取代下面的手工编译，见 4.3.4）。
3. **需要观察的现象**：两个 `zero?` 都输出 1；`usable` 四个值可能不同——`mi_usable_size(b)` 与 `mi_usable_size(c)` 相同（同为 100 字节请求、同 size class），而 `strdup` 的 usable 明显更小。
4. **预期结果**：验证「zalloc/calloc 清零、malloc 不清零、strdup 按实际长度分配」。如果 `is_zero(b,100)` 返回 0，说明链到了别的分配器（检查 `LD_LIBRARY_PATH`）。
5. 数值部分（usable 具体数字）**待本地验证**：不同平台指针宽度与 size class 表会导致差异。

#### 4.2.5 小练习与答案

**练习 1**：`mi_calloc(SIZE_MAX/2, 3)` 会发生什么？
**答案**：返回 NULL，不会「wrap around」分配一个小块。依据 [src/alloc.c:L291-L295](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L291-L295) 的 `mi_count_size_overflow` 检查；test-api.c 的 `calloc-overflow` 用例就是这条路径的回归测试。

**练习 2**：`mi_free(NULL)` 安全吗？
**答案**：安全。test-api.c 中 `malloc-free-null` 用例专门验证（见 [test/test-api.c:L108-L110](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c#L108-L110)），与标准 `free(NULL)` 语义一致。

**练习 3**：为什么 `mi_zalloc` 清零「几乎免费」？
**答案**（提示级答案，完整机制在单元四）：mimalloc 从 OS 拿到的新内存本身是零页；只有当块是从「别人用过的 free list」里弹出时才需要真正 memset。`mi_zalloc` 把 zero 标志传给内层（[src/alloc.c:L283-L285](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L283-L285)），由内层判断是否需要清。

### 4.3 test-api.c：官方的 API 用法示范

#### 4.3.1 概念说明

[test/test-api.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c) 是 `make test` 实际执行的程序（见 4.3.3 的构建证据），它把整个公共 API 表面按「几十个小用例」过了一遍。对学习者来说它是一座金矿：**每个用例就是一个官方认证的正确调用姿势**，还包括大量边界行为（分配 0 字节、传 NULL、尺寸溢出、非法对齐值等）。文件头部的注释（L11-L24）还解释了 mimalloc 的测试哲学——分配器 bug 往往只在特定分配序列下暴露，所以除了 API 表面测试，更依赖 debug 构建下的内部不变式检查与外部 mimalloc-bench 压测。

#### 4.3.2 核心流程

test-api.c 的所有用例都套在一个巧妙的小宏里。`CHECK_BODY(name)` 展开后是一个 `for` 循环：循环体执行一遍你的测试代码，循环变量 `result` 在循环结束时交给 `check_result` 打印 `ok.` 或 `FAILED` 并计数：

```text
CHECK_BODY("malloc-zero") {          // 打印 "test: malloc-zero...  "
  void* p = mi_malloc(0);            // ← 用例本体，给 result 赋值
  result = (p != NULL);
  mi_free(p);
};                                   // 循环收尾时 check_result(result,...) 打印判定
```

`main` 函数从上到下依次执行所有用例，最后 `return print_test_summary();` 输出 `succeeded: N / failed: M` 并以失败数为返回值——这也是 `ctest` 判定通过的依据。

#### 4.3.3 源码精读

- [test/testhelper.h:L17-L47](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/testhelper.h#L17-L47) —— `CHECK_BODY` 宏与 `print_test_summary` 的全部实现。`for(bool done=false, result=true; !done; done = check_result(...))` 这个「自增条件放在迭代表达式里」的写法，保证了用例体恰好执行一次且必然走到判定。
- [test/test-api.c:L100-L138](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c#L100-L138) —— malloc/calloc 基础用例群：`malloc-zero`（0 字节合法）、`malloc-nomem1`（请求 `PTRDIFF_MAX+1` 返回 NULL）、`malloc-free-null`、`calloc-overflow`、`malloc-large`（64MiB 大块）、`calloc0`（用 `mi_usable_size` 断言 0 计数分配的 usable ≤ 16）。这十几个用例就是 4.2 小练习的「官方答案页」。
- [test/test-api.c:L333-L338](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c#L333-L338) —— `zalloc-aligned-small1`：演示如何用文件内辅助函数 `mem_is_zero` 验证 `mi_zalloc_aligned` 确实清零。`mem_is_zero`/`mem_has_vals` 定义在 [test/test-api.c:L59-L68](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c#L59-L68)，可直接抄进自己的程序。
- [test/test-api.c:L404-L411](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c#L404-L411) —— `free_small1`：循环倍增尺寸，`mi_zalloc` 后写最后一个元素、`mi_free_size` 释放——这是「小对象 + 带尺寸释放」组合的官方示范。
- [CMakeLists.txt:L949-L971](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L949-L971) —— 构建证据：`foreach(TEST_NAME api api-fill stress-heaps stress-subprocs stress)` 为每个名字创建 `mimalloc-test-${TEST_NAME}` 可执行文件（源码 `test/test-${TEST_NAME}.c`），优先链 `mimalloc-static`，并注册为 ctest 条目。所以 `test-api.c` 的产物名是 `mimalloc-test-api`。

#### 4.3.4 代码实践

**实践目标**：跑通官方测试，并借它第一次看到统计输出。

1. 在构建目录运行测试：

   ```bash
   cd <仓库路径>/out/release
   ctest --output-on-failure        # 或: ./mimalloc-test-api
   ```

2. 直接带环境变量运行可执行文件：

   ```bash
   ./mimalloc-test-api
   ```

   终端会滚动输出几百行 `test: xxx...  ok.`，结尾是 `succeeded: N failed: 0`。
3. **需要观察的现象**：有无 `FAILED:` 行；最终 `failed` 是否为 0。
4. **预期结果**：全部 `ok.`、退出码 0。若个别用例失败，优先检查是否在非常规平台/容器环境（部分用例依赖大额内存申请，如 `arena_reserve` 需要预留 16GiB 虚拟地址空间）。
5. 具体用例数目 **待本地验证**（随版本变动）。

#### 4.3.5 小练习与答案

**练习 1**：`main` 第一行为什么是 `mi_option_disable(mi_option_verbose)`（[test/test-api.c:L73-L74](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c#L73-L74)）？
**答案**：verbose 默认在某些构建下可能开启，会在测试输出里混入大量分配器日志；测试希望输出干净、可比对，所以显式关掉。这也顺带示范了 `mi_option_disable` 的用法——任何环境变量都能被程序内的 option 调用覆盖。

**练习 2**：`malloc-aligned5` 用例里那行 `fprintf(stderr, ...)` 有什么可借鉴的？
**答案**：[test/test-api.c:L196-L202](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c#L196-L202) 在断言之外把 `mi_usable_size(p)` 的实际值打到 stderr。这是学习分配器的好习惯：断言只告诉你对错，多打印一行观测量（如 usable size）能告诉你「为什么」——对齐分配 4097 字节时 usable 会显著大于 4097，因为要对齐到 4096 边界导致过度分配。

### 4.4 MIMALLOC_SHOW_STATS：读懂分配器的体检报告

#### 4.4.1 概念说明

`MIMALLOC_SHOW_STATS=1` 是观察 mimalloc 行为的第一工具：程序退出时自动打印一份统计报表，涵盖「每个 size class 分了多少块、页的吞吐、arena 的内存收支、进程 RSS」。它的生效链条是：

```text
环境变量 MIMALLOC_SHOW_STATS=1
  → options.c 启动时读环境变量，把 mi_option_show_stats 置为 1
  → 程序退出走到 init.c 的清理流程
  → 检查到 show_stats 开启 → 调用统计打印（stats.c 的 _mi_stats_print）
```

**一个重要陷阱**：逐 bin 的统计（`bin` 行）属于「细粒度统计」，只有 `MI_STAT >= 2` 时才编译进去，而 `MI_STAT` 默认跟随 `MI_DEBUG`——**release 构建 `MI_STAT=0`，连 blocks 段都不打印**。想看到完整报表（含 bin 行），请用 **debug 构建**的库。这一点在源码小节给出证据。

#### 4.4.2 核心流程

报表由四个段组成，生成顺序（对应 `_mi_stats_print` 的代码结构）：

```text
subproc 0                                  ← 报表属主（进程/子进程号）
blocks 段   （MI_STAT>1 时）bin S 1, bin S 2, ... / binned / huge / total / malloc req
pages 段    touched / pages / abandoned / reclaima / reclaimf / ... / searches
arenas 段   reserved / committed / reset / purged / arenas / mmaps / commits / ... / theaps / heaps
process 段  threads / numa nodes / elapsed / user / sys / RSS / commit / page-faults
```

每一行的列含义固定（表头在 `mi_print_header`）：`peak`（峰值）、`total`（累计）、`current`（当前存量 = 累计分配 − 累计释放）、`block`（该行每块的字节数）、`total#`（累计次数）。行尾还会打印 `ok`（current 为 0，全部还了）或 `not all freed`。

#### 4.4.3 源码精读

**① 环境变量如何变成选项值**

- [src/options.c:L116-L120](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L116-L120) —— 选项表的开头几行，每行 `{ 默认值, MI_OPTION_UNINIT, MI_OPTION(选项名) }`。L120 正是 `show_stats`，默认 `0`（关闭）。前三行对应 mimalloc.h 里的三个稳定选项。
- [src/options.c:L624-L635](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L624-L635) —— 初始化时用 `_mi_getenv` 读取 `mimalloc_<name>` 形式的环境变量（大小写不敏感），写入选项值；遇到旧名称会打印弃用警告，值非法也会告警（L679-L686）。这就是 `MIMALLOC_SHOW_STATS=1` 的解析点。

**② 退出时在哪里触发打印**

- [src/init.c:L634-L638](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L634-L638) —— 进程收尾流程中：`if (mi_option_is_enabled(mi_option_show_stats) || mi_option_is_enabled(mi_option_verbose))` 成立时，先把各线程的统计合并到主 subproc（L635-L637 的三次 merge），再调用 `mi_subproc_stats_print_out` 输出整份报表。`MIMALLOC_VERBOSE=1` 也会连带打印统计——仓库自己的测试就这么用（[CMakeLists.txt:L981](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L981) 给 test-stress-static 配了 `MIMALLOC_VERBOSE=1 MIMALLOC_SHOW_STATS=1`）。

**③ 报表怎么打印出来的**

- [src/stats.c:L356-L430](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L356-L430) —— `_mi_stats_print` 主体：L367-L384 blocks 段、L386-L402 pages 段、L404-L422 arenas 段、L424-L427 process 段。注意 L368 与 L372 的条件编译——这决定了 release 下看不到 blocks 段。
- [include/mimalloc/types.h:L80-L87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L80-L87) —— **MI_STAT 的默认规则**：`MI_DEBUG>0` 时 `MI_STAT=2`，否则 `MI_STAT=0`。也就是说：debug 构建自动获得细粒度统计；release 构建默认完全没有分配量统计（性能优先）。
- [src/stats.c:L277-L295](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L277-L295) —— `mi_stats_print_bins`：遍历所有 bin，只打印 `total > 0` 的行；标签格式 `"bin%2s %3lu"`，中间的 `%2s` 是页类别字母——按该 bin 的块尺寸判断：`S`（small）≤ `MI_SMALL_MAX_OBJ_SIZE`、`M`（medium）≤ `MI_MEDIUM_MAX_OBJ_SIZE`、`L`（large）≤ `MI_LARGE_MAX_OBJ_SIZE`、`H`（huge）。
- [src/stats.c:L197-L236](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L197-L236) —— 每行的列输出逻辑 `mi_stat_print_ex`：依次打印 peak、total、current、unit、次数；L221-L228 行尾根据 `current != 0` 打印 `not all freed` 或 `ok`。这就是「泄漏一眼可见」的来源——程序正常退出后仍 `not all freed` 的行就是嫌疑对象。
- [src/stats.c:L151-L185](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L151-L185) —— 数值的单位换算 `mi_printf_amount`：统计用二进制单位（基数为 \(2^{10}=1024\)），所以你会看到 `KiB`、`MiB`、`GiB` 后缀；`n * unit` 的乘法在 L159 完成——bin 行的 current 列是「块数 × 块大小」折算的字节数，而最右 `total#` 列才是纯块数。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：完成规格指定的任务——用四种接口分配再释放，逐行解读统计输出。

1. 准备程序 `stat-mi.c`（示例代码；也可直接复用 4.2.4 的 `cycle-mi.c`，但这里把尺寸拉开以便观察不同 bin）：

   ```c
   #include <stdio.h>
   #include <mimalloc.h>

   int main(void) {
     void* p1 = mi_malloc(16);        // 很小的块 → 低号 bin
     void* p2 = mi_zalloc(64 * 1024); // 64KiB → medium 档
     void* p3 = mi_calloc(1024, 1024);// 1MiB → large 档
     char* p4 = mi_strdup("stats");   // 6 字节 → 与 p1 相邻的 bin
     printf("p1=%p p2=%p p3=%p p4=%p\n", p1, p2, p3, p4);
     mi_free(p1); mi_free(p2); mi_free(p3); mi_free(p4);
     return 0;
   }
   ```

2. **用 debug 构建的库编译**（关键！原因见 4.4.1 与 types.h 的 MI_STAT 规则），然后运行：

   ```bash
   MIMALLOC_SHOW_STATS=1 ./stat-mi
   ```

3. **需要观察的现象**：退出时打印的报表中——
   - blocks 段应出现若干 `bin S n` 行（`n` 是 16、6、64KiB 对应的 bin 编号）和一条 medium/large 相关行；`p3`（1MiB）大概率落在 `huge` 行或独立的大尺寸 bin，**待本地验证**；
   - 所有行行尾应为 `ok`（因为我们全部释放了）；
   - pages 段能看到 `touched`（触碰过的页内存）与 `pages` 计数；
   - arenas 段能看到 `reserved`/`committed`（u1-l1 讲过的 arena 默认 1GiB 预留在这里体现）；
   - process 段有 RSS 与 page-faults。
4. **解读任务**（拿实际输出对照填空）：

   | 输出片段 | 含义 |
   | --- | --- |
   | `bin S  3 : ... 16 B ... 1  ok` | 第 3 号 bin、块大小 16 字节、本次运行累计分配 1 块、退出时已全部释放 |
   | `bin M 40 : ...` | medium 档第 40 号 bin，来自 64KiB 的 `mi_zalloc` |
   | `total : ... not all freed` | 若出现，说明有块没还——检查是否漏了某个 `mi_free` |
   | `touched`（pages 段） | 实际写过的页内存，是碎片化程度的粗指标 |
   | `reserved / committed`（arenas 段） | 虚拟预留 vs 已提交给 OS 的内存 |

5. **对照实验**：改用 release 构建的库重跑 `MIMALLOC_SHOW_STATS=1 ./stat-mi`，对比发现 blocks 段消失——用 [include/mimalloc/types.h:L80-L87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L80-L87) 的规则解释这一差异。
6. **预期结果**：能指着输出说出「哪个数字对应程序里哪一次分配」。具体 bin 编号与数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：不重新编译，把「打印统计」永久开进程序里怎么做？
**答案**：调用 `mi_option_enable(mi_option_show_stats);`（声明在 [include/mimalloc.h:L523-L533](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L523-L533)）。它与设置环境变量走同一个选项存储，程序内设置的优先级更高（test-api.c 第一行关闭 verbose 就是证明）。

**练习 2**：为什么 bin 行里 `S`/`M`/`L`/`H` 四个字母是理解 mimalloc 尺寸体系的钥匙？
**答案**：它们对应四个尺寸档位（small/medium/large/huge），档位边界由 `MI_SMALL_MAX_OBJ_SIZE` 等宏定义，判别逻辑就在 [src/stats.c:L283-L287](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L283-L287)。看懂字母就能把「请求尺寸 → bin 编号 → 页类型」串起来，这是单元三、单元四的伏笔。

**练习 3**：报表里 `current` 列在什么情况下不为 0？
**答案**：`current = total − freed`，即退出时仍未释放的存量。故意注释掉 4.4.4 程序里的一个 `mi_free` 再跑：对应 bin 行的 current 变为该块大小（字节数），行尾变成 `not all freed`——这正是用统计报表排查泄漏的最小实验。

## 5. 综合实践

**任务：做一个 10 行代码的「分配器行为探测器」。**

综合运用本讲全部内容，写一个程序 `probe.c`：

1. 包含 `mimalloc.h`，`main` 开头调用 `mi_option_enable(mi_option_show_stats)`（练习 4.4-1 的做法）。
2. 用 `mi_good_size` 打印一张小表：对请求尺寸 1、4、16、64、256、1024、4096、65536、1048576 字节，输出 `mi_good_size(n)` 的值（不真的分配）。
3. 真实分配这些尺寸各一块，打印每个指针的 `mi_usable_size`，与 `mi_good_size` 的预测对照，验证两者一致。
4. 全部释放后正常退出，读取报表，确认所有 bin 行行尾都是 `ok`。
5. 思考题（带着问题进入下一讲）：`mi_good_size` 输出的序列为什么不是连续的？这些「台阶」就是 size class 表——下一讲我们去看它在源码里的真身。

**验收标准**：`mi_usable_size` 与 `mi_good_size` 对每个尺寸完全一致；统计报表无 `not all freed`。（台阶的具体数值随平台变化，以本地输出为准。）

## 6. 本讲小结

- `mimalloc.h` 是唯一的公共头文件，按「标准接口 → 扩展 → 对齐 → 类型化 → 堆 → 选项 → 适配」分区，L11 的 `MI_MALLOC_VERSION` 标明 API 版本（当前 30500 = v3.5.0）。
- `mi_malloc` / `mi_zalloc` / `mi_calloc` / `mi_strdup` 的入口实现都只有一行——先取当前线程默认 theap 再进内层（[src/alloc.c:L256-L258](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L256-L258) 等）；`mi_calloc` 多一步乘法溢出检查；释放统一 `mi_free`。
- `test/test-api.c` 是官方用法词典：`CHECK_BODY` 宏让每个用例恰好执行一次并自动判定；构建产物名为 `mimalloc-test-api`。
- 统计链路：环境变量在 [src/options.c:L624-L635](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L624-L635) 解析 → 退出时 [src/init.c:L634-L638](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L634-L638) 触发 → [src/stats.c:L356-L430](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L356-L430) 分四段打印。
- bin 行 = 一个 size class 的收支记录，`S/M/L/H` 标记尺寸档位，五列依次是 peak / total / current / block / total#，行尾 `ok` 或 `not all freed` 直接暴露泄漏。
- 细粒度统计（含 bin 行）需要 `MI_STAT>=2`，它默认只在 debug 构建开启——**看完整报表请用 debug 库**。

## 7. 下一步学习建议

本讲你已经会「用」mimalloc 并能读它的统计了。接下来：

- **下一讲（u2-l1）**：学习不改一行代码替换系统 `malloc` 的三种方式（`LD_PRELOAD`、静态目标文件优先链接、`mimalloc-override.h` 宏替换），把本讲的 `mi_` 调用与标准 `malloc` 调用两套世界连通。
- **源码预读**：如果等不及单元三，可以先看 [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) 中各结构体定义处的注释——本讲反复出现的 theap、page、bin 都能在那里找到字段级说明。
- **观察力迁移**：以后写任何 C 程序，习惯性地用 `MIMALLOC_SHOW_STATS=1` 跑一遍 debug 构建，看 `not all freed` 行——这是零成本 leaks-asan 平替的起点（完整观测工具链见 u9）。
