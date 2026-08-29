# 工具链与基准：valgrind/ASAN/ETW 与性能评估

## 1. 本讲目标

本讲是单元九的最后一讲，也是整本手册的收尾。前面九讲我们把这个分配器「由内而外」拆了一遍，本讲回答两个工程问题：

1. **怎么证明自己没写错？** 掌握 `MI_TRACK_VALGRIND` / `MI_TRACK_ASAN` / `MI_TRACK_ETW` 三种插桩构建的原理与用法：mimalloc 如何通过 [include/mimalloc/track.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L1-L150) 里的一套宏，把「块诞生 / 块死亡 / 内存三态」申报给 Valgrind、AddressSanitizer 这类外部内存检查工具，并用 [test/test-wrong.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L1-L99) 这把「故意出错」的验收尺验证效果。
2. **怎么证明它更快？** 读懂 readme 的基准方法论（cfrac、leanN、xmalloc-testN 等每个场景到底在压什么），把各基准的强弱项映射回我们已学过的设计根源（free list 分片、一次 CAS 的跨线程 free、abandon/reclaim），并独立完成一次「系统 malloc vs `LD_PRELOAD=mimalloc`」的 A/B 对比测试。

## 2. 前置知识

### 2.1 内存检查工具是靠「影子状态」工作的

Valgrind（memcheck）和 AddressSanitizer（ASAN）的核心思路相同：为进程的每一段内存维护一份**影子状态**，在每次读写发生时检查「这次访问对这段内存的当前状态是否合法」。差别只在实现位置：

- **Valgrind**：动态二进制翻译，逐条指令插桩，无需重新编译程序。它对内存维护三态：`defined`（已写入过，可读）、`undefined`（已分配但未初始化，读它会被告警）、`noaccess`（不属于任何合法对象，碰它就是越界）。
- **ASAN**：编译期插桩加运行时库，把影子状态放在 **影子内存** 里，`poison`（下毒）表示不可访问、`unpoison`（解毒）表示可访问。代价是程序必须和 `-fsanitize=address` 一起编译。

### 2.2 自定义分配器为什么必须「主动适配」这些工具

这两类工具默认靠**拦截 `malloc`/`free` 符号**来记账。但 mimalloc 是从 OS 整块批发内存（arena + mmap，见 u6），再自己切片零售。如果不适配，工具只会看到「mimalloc 拿走了一大块」，完全不知道里面成千上万个用户块的生死，结果是：

- 越界读写**检不出**——工具以为整块 arena 都是一个巨大对象；
- 释放后的 use-after-free **检不出**——块被归还给 free list 复用时，工具毫不知情；
- 泄漏**误报**——所有块都被算进那一大块「未释放内存」里。

所以分配器必须在正确的时机「打电话」给工具：块诞生时申报区间、块死亡时销账、块进入 free list 时把区间标成不可访问。这套「电话簿」就是 track.h。

### 2.3 宏合同，而不是函数接口

mimalloc 选择用**宏**而不是回调函数做适配层：release 构建下整组宏展开为空（[track.h:L92-L102](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L92-L102)），做到**真正的零开销**，而不是「一次间接调用的开销」。这和 u1-l2 讲过的「cmake 选项 → C 宏 → 源码 `#if` 分支」构建链路、u2-l1 讲过的 `LD_PRELOAD` 覆盖机制是同一条方法论。

### 2.4 本讲要继承的前序结论

- u9-l1/u9-l2：debug 构建的 padding canary 在 **free 时刻** 抓「写穿」与 double free；本讲会看到外部工具在 **访问时刻** 抓错误，连「读越界」都能抓——两张检测网互补。
- u8-l2：free list sharding 与 multi-sharding 是 mimalloc 并发优势的根源，基准一节的结论要拿它来解释。
- u1-l2：构建目录名与 `MI_SECURE`/`MI_DEBUG` 等开关的联动方式。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/mimalloc/track.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L1-L150) | 分配器 ↔ 内存检查工具的适配层：一组 `mi_track_*` 宏合同 + valgrind/asan/ETW/none 四个后端 |
| [test/test-wrong.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L1-L99) | 故意犯遍九类内存错误的「验收尺」，头部注释给出 valgrind/ASAN 的完整构建命令 |
| [CMakeLists.txt](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L24-L25) | `MI_TRACK` 选项链路：头文件探测、`-fsanitize=address` 注入、库名后缀、追踪下跳过的测试目标 |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L28-L78) / [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L149-L152) / [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L709-L717) / [src/os.c](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L324-L336) | `mi_track_*` 宏的真实调用点：块生死申报与内存三态标注 |
| [src/alloc-override.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L29-L47) / [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1406-L1414) | 追踪构建的全局副作用：malloc 别名技巧被禁用、`rep movsb` 内联汇编被禁用 |
| [test/test-stress.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L7-L17) | 多线程压力测试：模拟真实负载特征，但**明确声明不能当基准用** |
| [readme.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L571-L662) | Tools 一节（valgrind/ASAN/ETW 用法）与 Performance 一节（基准方法论与各场景解读） |

## 4. 核心概念与源码讲解

### 4.1 track.h：一套宏合同，三个插桩后端

#### 4.1.1 概念说明

track.h 解决的问题是：**让外部内存检查工具看穿 mimalloc 的内部批发-零售结构**。它定义了一组语义固定的宏，`alloc.c`/`free.c` 等实现代码只调用这套宏、完全不关心后端是谁；后端由编译期宏三选一（加 none 共四种形态）：

- `MI_TRACK_VALGRIND` → 展开为 `VALGRIND_MALLOCLIKE_BLOCK` 等客户端请求；
- `MI_TRACK_ASAN` → 展开为 `ASAN_POISON/UNPOISON_MEMORY_REGION`；
- `MI_TRACK_ETW` → 展开为 Windows 事件写入（性能分析用途，而非错误检测）；
- 都没定义 → 全部展开为空，零开销。

宏协议分两组：**分配记账**（`mi_track_malloc_size` / `mi_track_free_size`，块生死申报）与**内存三态**（`mi_track_mem_defined` / `mi_track_mem_undefined` / `mi_track_mem_noaccess`，区间状态标注）。

#### 4.1.2 核心流程

分配与释放两条主链路上的申报点（均为 u4/u5 精读过的路径）：

```text
分配:  mi_theap_malloc_small_zero_nonnull        (alloc.c)
         └─ mi_page_malloc_zero                   # 从页的 free list 弹出一个块
              └─ mi_track_malloc(p, size, zero)   # ★ 块诞生：向工具申报 [p, p+size)

释放:  mi_free_block_local / mi_free_block_mt    (free.c)
         └─ mi_track_free_size(block, usable_size)# ★ 块死亡：向工具销账同一区间
```

三态标注则发生在更细的粒度：

- 新页初始化时整页区间先标 `noaccess`（[page.c:L717](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L717)），块被弹出后才 `defined`/`undefined`；
- 块进入 free list 后，其头 8 字节要复用存 `next` 指针（u3-l2），于是先 `defined` 再写入；
- OS 层拿到零页时依 `is_zero` 标 `defined`（零页读出来是确定的 0）或 `undefined`。

一个关键的**尺寸合同**：传给 `mi_track_free_size` 的 size 必须与当初 `mi_track_malloc_size` 申报的 size 完全一致（目前即 `mi_usable_size(p)`），否则工具的区间账本会对不上，留下永久误报。是否**字节精确**取决于 `MI_PADDING`：

- `MI_PADDING` 开（debug/secure 构建，u9-l2）：`size == reqsize`，逐字节精确；
- `MI_PADDING` 关（release 构建）：`size` 是块的可用尺寸，可能大于请求，越界检查存在盲区（块内取整缝隙）。

#### 4.1.3 源码精读

先看文件头部的合同注释，它把两组宏的语义和尺寸约定写得非常清楚：

- [include/mimalloc/track.h:L15-L24](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L15-L24) —— 定义分配记账宏清单，并约定「free 申报的 size 永远与 malloc 申报的匹配；`MI_PADDING` 开启时字节精确（`size==reqsize`），否则用可能更大的可用块尺寸」。
- [include/mimalloc/track.h:L37-L42](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L37-L42) —— 定义内存三态宏：`defined` / `undefined` / `noaccess`。

四个后端分支是四选一的 `#if` 阶梯：

**Valgrind 后端**（[track.h:L46-L61](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L46-L61)）：把每个用户块包装成一个 Valgrind 眼中的「malloc-like 块」，红区大小取 `MI_PADDING_SIZE`；`MAKE_MEM_*` 三态与 Valgrind 的三态一一对应，表达力最完整。同时置 `MI_TRACK_HEAP_DESTROY=1`（注释：在 theap 销毁时逐块 track free）。

**ASAN 后端**（[track.h:L63-L76](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L63-L76)）：只有「可访问 / 不可访问」两态，于是 `mem_defined` 与 `mem_undefined` **都**映射成 `ASAN_UNPOISON_MEMORY_REGION`（L74-L75），`noaccess` 映射成 `ASAN_POISON_MEMORY_REGION`。也就是说 ASAN 后端不追踪「未初始化」这个维度——那需要 MemorySanitizer，超出本宏合同。

**ETW 后端**（[track.h:L78-L90](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L78-L90)）：Windows 事件追踪，`mi_track_malloc_size`/`mi_track_free_size` 变成 `EventWriteETW_MI_ALLOC/FREE` 事件写入，供 Windows Performance Analyzer 离线分析**分配行为画像**——它不做错误检测，是性能观测工具。它独占 `mi_track_init`/`mi_track_done` 两个生命周期钩子（事件的注册与注销）。

**none 后端**（[track.h:L92-L102](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L92-L102)）：两个记账宏为空，`MI_TRACK_ENABLED=0`。

每个后端只需覆盖自己关心的宏，其余由兜底层补默认值（[track.h:L107-L133](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L107-L133)），例如 `mi_track_align` 的默认实现是把对齐块的前导偏移标成 `noaccess`。最后是带断言的安全包装（[track.h:L136-L148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L136-L148)）：`MI_PADDING` 开启时断言 `mi_usable_size(p)==reqsize`（字节精确），否则断言 `>=`，再把尺寸传给后端——这段代码把 4.1.2 的尺寸合同变成了可执行的契约。

再看三个真实调用点，感受宏如何嵌进主链路：

- [src/alloc.c:L149-L152](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L149-L152) —— 小对象快路径拿到块之后立刻 `mi_track_malloc(p,size,zero)` 申报诞生，位置就在 u4-l1 数过的那几条指令之后。
- [src/free.c:L35-L38](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L35-L38) 与 [src/free.c:L69-L70](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L69-L70) —— 本线程与跨线程两条释放路径都在 padding 校验通过后调用 `mi_track_free_size(block, usable_size)` 销账，然后才动 free list。
- [src/os.c:L324-L336](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L324-L336) —— OS 层新映射一段内存时，`#ifdef MI_TRACK_ASAN` 分支按 `is_zero` 标 `defined`/`undefined`，注释直言「seems needed for asan」：这是为让 ASAN 构建通过 `mimalloc-test-api` 而打的补丁，说明影子状态与零页语义必须对齐。

生命周期钩子的两端在 init.c：进程初始化末尾 `mi_track_init()`（[src/init.c:L565](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L565)，只有 ETW 后端有实际动作），进程退出清理中途 `mi_track_done()`（[src/init.c:L620](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L620)）。

另外注意一个诚实的事实：`MI_TRACK_HEAP_DESTROY`（[track.h:L50](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L50)）在 v3.5 源码里**只有定义、没有读取者**（全仓库检索仅命中 track.h 自身），应视为历史遗留——再次印证 u3-l2 的教训：断言与合同是权威，注释和宏可能滞后。

#### 4.1.4 代码实践

**实践目标**：亲手构建一个追踪版 mimalloc，并从两个独立证据确认「追踪真的开着」。

**操作步骤**：

1. 在仓库根目录执行：
   ```bash
   mkdir -p out/asan && cd out/asan
   cmake ../.. -DMI_TRACK=ASAN -DCMAKE_BUILD_TYPE=Debug
   make -j8
   ```
2. 观察配置阶段输出应出现 `Compile with address sanitizer support (MI_TRACK=ASAN)`（来自 [CMakeLists.txt:L324](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L324)）。
3. 检查产物名：应为 `libmimalloc-asan-debug.a` / `libmimalloc-asan-debug.so`（命名规则见 4.2.3）。
4. 任取一个链接了该库的小程序，加 `MIMALLOC_VERBOSE=1` 运行，在选项转储末尾的「build configuration」段找 `mem tracking:` 一行——它打印的正是宏 `MI_TRACK_TOOL`（[src/options.c:L242-L245](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L242-L245)），应显示 `asan`。

**需要观察的现象**：库名多了 `-asan` 后缀；verbose 输出出现 `mem tracking: asan`。

**预期结果**：两个证据互相印证追踪后端已编入。**待本地验证**（本讲义未替你执行这些命令）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mi_track_free_size` 申报的 size 必须与 `mi_track_malloc_size` 严格一致？
**答案**：工具按地址区间记账。free 申报的区间若小于 malloc 申报的区间，多出来的部分永远处于「已分配」状态，程序明明没泄漏却报泄漏；反之则可能把相邻块的合法内存错误解毒。所以合同规定两者恒等（当前即 `mi_usable_size(p)`）。

**练习 2**：ASAN 后端为什么把 `mem_defined` 和 `mem_undefined` 都映射成 `ASAN_UNPOISON_MEMORY_REGION`？
**答案**：ASAN 的手工影子操作只有可访问/不可访问两态，没有「已分配但未初始化」这一维度；「读未初始化内存」的检测属于 MemorySanitizer 的职责。因此 `undefined` 在 ASAN 后端只能降级为「可访问」（[track.h:L74-L75](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/track.h#L74-L75)），而 Valgrind 后端保留了完整三态。

**练习 3**：release 构建（无 `MI_PADDING`）下追踪构建的越界检测存在什么盲区？
**答案**：申报尺寸是可用块尺寸而非请求尺寸，块尾的取整缝隙（最多到下一个 size class 边界）内的小幅越界落在「已申报」区间内，工具不会报。开 `MI_PADDING` 后申报变为字节精确，缝隙被纳入 `noaccess`，越界立即暴露。

### 4.2 MI_TRACK 构建链路与追踪构建的全局副作用

#### 4.2.1 概念说明

本模块讲两件事：`MI_TRACK` 选项如何从 cmake 一路传导到源码宏；以及一个容易被忽视的事实——**追踪构建不只是「加了几个宏」，它改变了库的代码形态**。别名覆盖被禁用、debug 魔数填充被关闭、内联汇编优化被关闭。理解这一点才能回答「为什么性能测试绝不能用追踪构建」。

#### 4.2.2 核心流程

选项传导链（承接 u1-l2 的「cmake 选项 → C 宏 → 源码分支」）：

```text
cmake -DMI_TRACK=ASAN                (或旧式 -DMI_TRACK_ASAN=ON，被映射)
  → mi_defines 追加 MI_TRACK_ASAN=1   (CMakeLists.txt L325)
  → 同时给编译/链接加 -fsanitize=address (L326-L327)
  → include/mimalloc/track.h 的 #elif MI_TRACK_ASAN 分支生效
  → 库名追加 -asan 后缀 (L766-L768)，再叠加构建类型 -debug
```

#### 4.2.3 源码精读

**新旧两代选项**。新选项是一个字符串缓存变量，取值 `OFF/ASAN/VALGRIND/ETW`（[CMakeLists.txt:L24-L25](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L24-L25)）。旧的三个布尔选项 `MI_TRACK_VALGRIND/ASAN/ETW` 被标记为 Deprecated（[CMakeLists.txt:L80-L82](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L80-L82)），但仍被接受：开启时打印弃用警告并翻译成 `MI_TRACK` 的对应值（[CMakeLists.txt:L292-L301](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L292-L301)）。注意 readme 的 Tools 一节和 test-wrong.c 的头部注释用的都还是**旧写法**（如 `-DMI_TRACK_VALGRIND=ON`），照抄能跑，但会看到弃用提示——这是「文档滞后于代码」的又一实例。

**三个后端的前置检查**：

- Valgrind：先探测 `valgrind/valgrind.h` 与 `memcheck.h` 是否存在，找不到就整档回退 `OFF` 并警告「先安装 valgrind」（[CMakeLists.txt:L303-L312](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L303-L312)）。
- ASAN：macOS 上若 `MI_OVERRIDE` 同时开启则直接回退（[CMakeLists.txt:L313-L316](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L313-L316)）；探测到 `sanitizer/asan_interface.h` 后，除定义宏外还把 `-fsanitize=address` 同时追加进编译参数与链接参数（[CMakeLists.txt:L318-L328](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L318-L328)）——链接侧必须加，否则找不到 ASAN 运行时。
- ETW：非 Windows 直接回退（[CMakeLists.txt:L330-L337](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L330-L337)）。

**库名后缀**：追踪构建的库名会带上后缀（[CMakeLists.txt:L759-L768](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L759-L768)），`MI_TRACK=VALGRIND` 追加 `-valgrind`、`=ASAN` 追加 `-asan`，再按 u1-l2 的规则叠加 `-debug`/`-secure`，最终形如 `libmimalloc-asan-debug.a`——test-wrong.c 头部注释里的链接命令引用的正是这个名字。

**副作用一：malloc 别名覆盖被禁用**。[src/alloc-override.c:L29-L47](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L29-L47) 中，GCC/Clang 下用 `__attribute__((alias))` 让 `malloc` 与 `mi_malloc` 同地址的零开销技巧，条件里显式排除了 `MI_TRACK_ENABLED`：追踪构建退化为「真函数调用转发」（`{ return fun(x); }`）。u2-l1 讲过的符号抢占结构在追踪构建下形态不同——插桩工具自身要靠拦截这些标准符号工作，别名合并会让两套拦截互相踩踏（此为依据代码行为的合理推断，源码未写明原因）。

**副作用二：debug 魔数填充被关闭**。u9-l2 讲过 debug 构建会用 `MI_DEBUG_UNINIT`/`MI_DEBUG_FREED` 魔数填充新块/死块。这些填充在追踪构建下被整体跳过：分配侧（[src/alloc.c:L90-L92](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L90-L92)）、本地释放侧（[src/free.c:L39-L42](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L39-L42)）、跨线程释放侧（[src/free.c:L73-L78](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L73-L78)）的 `#if` 都带 `!MI_TRACK_ENABLED`。原因其一是填充会改写块内被复用作 free list 链接的内存，与工具观察到的影子状态冲突（L73 的注释还特别指出追踪下多线程不能调 `mi_usable_size`）；其二是把死块整体写成魔数这件事本身，工具会视为对已销账区间的非法写。

**副作用三：`rep movsb` 优化被关闭**。[include/mimalloc/internal.h:L1406-L1414](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1406-L1414) 在 x86/x64 上用内联汇编实现 `rep movsb/stosb` 版 memcpy 的快速路径，条件同样排除 `MI_TRACK_ENABLED`——内联汇编对编译器是不透明盒子，会绕过 ASAN 的访问插桩，追踪构建只能回退到可插桩的常规实现。

**副作用四：部分测试目标被跳过**。ASAN 构建下 `mimalloc-test-stress-static`（`mimalloc.o` 优先链接的静态覆盖测试）与 `mimalloc-test-stress-dynamic`（`LD_PRELOAD` 动态覆盖测试）都被条件排除（[CMakeLists.txt:L975-L989](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L975-L989)）——ASAN 自己重定向标准分配函数，与「用 mimalloc 抢占 malloc」这两件事在同一测试里互斥。

#### 4.2.4 代码实践

**实践目标**：用符号表亲眼确认「追踪构建禁用了 malloc 别名」。

**操作步骤**：

1. 分别构建普通版与 ASAN 版共享库（两个独立目录）。
2. 对比两个库导出符号的地址：
   ```bash
   nm -D out/release/libmimalloc.so    | grep -E ' (malloc|mi_malloc)$'
   nm -D out/asan/libmimalloc-asan-debug.so | grep -E ' (malloc|mi_malloc)$'
   ```

**需要观察的现象**：普通版里 `malloc` 与 `mi_malloc` 的地址**相同**（别名）；ASAN 版里两者地址**不同**（真转发函数）。

**预期结果**：与 alloc-override.c 的 `#if` 分支一一对应。若 `nm` 不可用，改用 `MI_SEE_ASM=ON`（u4-l1 介绍过）生成汇编清单对比调用形态亦可。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CMakeLists 在 ASAN 分支把 `-fsanitize=address` 同时加进 `mi_cflags` 与 `mi_libraries`？
**答案**：前者让 mimalloc 自身的源码被插桩（`mi_track_*` 调用的 `ASAN_POISON` 接口可用），后者让最终链接带上 ASAN 运行时库（影子内存初始化、错误报告都在运行时里）。缺一个都无法工作。

**练习 2**：macOS 上「`MI_OVERRIDE=ON` + ASAN」为什么被 cmake 直接回退成 `MI_TRACK=OFF`？
**答案**：ASAN 依赖重定向标准分配函数来拦截 `malloc`/`free`，而 `MI_OVERRIDE` 要求 mimalloc 导出同名符号抢占系统实现，两者在同一进程里抢同一批符号。cmake 的处理（[CMakeLists.txt:L314-L316](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L314-L316)）与 readme 的建议（macOS 需 `-DMI_OVERRIDE=OFF`，[readme.md:L640-L641](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L640-L641)）是同一取舍的两面。

**练习 3**：能否用 `libmimalloc-asan-debug.so` 做性能对比测试？
**答案**：不能。追踪构建下别名覆盖退化、memcpy 内联汇编优化关闭、每次分配/释放多出影子状态维护——它是一套不同的代码形态，性能数据不具参考性。性能测试必须用不带追踪的 release 构建（4.4 的实践也正是这么做的）。

### 4.3 test-wrong.c：一把故意出错的验收尺

#### 4.3.1 概念说明

[test/test-wrong.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L1-L99) 是一个不到百行的小程序，却在 main 里**把常见内存错误犯了个遍**。它的用途不是「测试 mimalloc 不出错」，而是「验证检测手段真的能报错」：换任何一种检测方案（ASAN、valgrind、debug padding、guarded），拿它跑一遍就知道该方案能抓哪些、抓不到哪些。它是 4.1/4.2 所学内容的**验收尺**。

#### 4.3.2 核心流程

程序顺序执行九类错误，覆盖「读 / 写 / 生命周期 / 泄漏」四个维度：

| # | 错误类型 | 源码位置 | 检测原理（外部工具） |
| --- | --- | --- | --- |
| 1 | 字节级越界**读**（`c[4]`、`c[-1]`） | [test-wrong.c:L65-L67](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L65-L67) | 访问时刻查影子状态：越界区间是 `noaccess`/poison |
| 2 | double free（`c`） | [test-wrong.c:L70](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L70) | 第二次 free 的区间已被标为不可访问 |
| 3 | 读未初始化内存（`*q`） | [test-wrong.c:L73-L74](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L73-L74) | Valgrind 的 `undefined` 三态（ASAN 后端不检测此项，见练习 4.1-2） |
| 4 | 字级越界读（`q[1]`、`q[-1]`） | [test-wrong.c:L77](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L77) | 同 #1 |
| 5 | 缓冲区溢出**写**（`q[1]=43`、`q[2]=44`） | [test-wrong.c:L82-L83](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L82-L83) | 写 poisoned 区间，当场报告 |
| 6 | 下溢写（`q[-1]=41`） | [test-wrong.c:L86](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L86) | 同 #5，方向相反 |
| 7 | double free（`q`） | [test-wrong.c:L91](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L91) | 同 #2 |
| 8 | use after free（`*q`） | [test-wrong.c:L94](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L94) | free 时已 poison，读访问被拒 |
| 9 | 泄漏（`p` 从不释放） | [test-wrong.c:L96-L98](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L96-L98) | 工具退出时对账：仍「在册」的块即泄漏 |

程序还留了一个对照开关：`USE_STD_MALLOC` 宏把 `mi(malloc)` 展开成系统 `malloc` 或 `mi_malloc`（[test-wrong.c:L49-L53](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L49-L53)），可以让同一份错误代码在「工具原生拦截系统 malloc」与「mimalloc 适配层」两种模式下分别受检。

#### 4.3.3 源码精读

- 头部注释就是官方操作手册：Valgrind 路线给出三步命令——`out/debug` 目录下 `cmake ../.. -DMI_TRACK_VALGRIND=1`、`make`，再用 `libmimalloc-valgrind-debug.a` 链接本文件，最后 `valgrind ./test-wrong`（[test-wrong.c:L10-L24](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L10-L24)）；ASAN 路线额外要求编译本文件时也带 `-fsanitize=address -fsanitize-recover=address`，并用 `ASAN_OPTIONS=verbosity=1:halt_on_error=0` 运行（[test-wrong.c:L27-L41](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L27-L41)）。`-fsanitize-recover` 是关键：默认 ASAN 首错即终止，加恢复选项才能让程序跑完整个错误清单、一次看全九份报告。
- 错误主体集中在四十行 main 里（[test-wrong.c:L55-L99](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L55-L99)），顺序刻意安排成「先读后写、先单后双、最后泄漏」，与上表一一对应。
- 一个容易被忽略的细节：[test-wrong.c:L61-L62](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L61-L62) 的 `mi_malloc_aligned`/`mi_free` **没有**包在 `mi(...)` 宏里——即使定义了 `USE_STD_MALLOC`，这两行仍走 mimalloc（它们本来也是一对正确的分配释放，只是提醒读者：读宏展开时要逐行核对）。
- 它同时是常规构建目标：`test/CMakeLists.txt` 把它链到 `mimalloc` 共享库（[test/CMakeLists.txt:L54-L56](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt#L54-L56)），所以普通构建里也存在，只是没人报错而已。

**与 u9-l1/u9-l2 检测网的对照**：debug 构建的 padding canary 在 **free 时刻** 校验（[src/free.c:L32](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L32) 与 [L66](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L66) 的 `mi_check_padding_on_free`），只能抓「写穿 canary 的越界**写**」与 double free；而 `c[4]` 这种越界**读**不修改任何内存，canary 完好无损——只有影子内存类工具（ASAN/valgrind）与 guarded 守卫页（写时 SIGSEGV）才能覆盖读越界。三张网各管一段，这正是 test-wrong 存在的价值：拿它一跑，哪张网漏了什么一目了然。

#### 4.3.4 代码实践（本讲核心实践之上半）

**实践目标**：同一份错误程序，在「无追踪」与「ASAN」两种构建下运行，直观对比检测能力的差距。

**操作步骤**：

1. 构建 ASAN 版（4.1.4 已完成）并手工编译 test-wrong（照抄头部注释的命令）：
   ```bash
   cd out/asan
   clang -g -o test-wrong -I../../include ../../test/test-wrong.c \
         libmimalloc-asan-debug.a -lpthread \
         -fsanitize=address -fsanitize-recover=address
   ASAN_OPTIONS=halt_on_error=0 ./test-wrong
   ```
2. 再构建一个普通 release 版，编译并直接运行同一份 test-wrong（不带任何 sanitizer 参数）。
3. （可选）按 [readme.md:L604-L608](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L604-L608) 的组合命令跑一次 valgrind + `LD_PRELOAD`。

**需要观察的现象**：普通版多半「安静地」跑完退出（错误全部漏检；若用 debug 版，free 时刻的 padding 校验可能抓到 #2/#5/#7 等写类错误）；ASAN 版则逐条打印错误报告，每份报告含错误类型、栈回溯与块地址。

**预期结果**：按 4.3.2 的表格逐项核对——ASAN 应报出 #1、#2、#4、#5、#6、#7、#8、#9（#3「读未初始化」不在其列，见练习 4.1-2）。**待本地验证**：具体哪几条被报、报告顺序如何，以你的实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`c[4]` 的越界读为什么 debug padding 检测不到？
**答案**：padding canary 是被动证据——只有被「写穿」才会损坏，free 时校验才能发现（u9-l2）。读操作不改变内存，canary 完好，校验自然通过。检测读越界需要访问时刻的检查（ASAN/valgrind 影子状态）。

**练习 2**：为什么编译 test-wrong 时要加 `-fsanitize-recover=address` 并设 `halt_on_error=0`？
**答案**：ASAN 默认在第一个错误处终止进程，九类错误只能看到第一条；恢复模式允许「记录后继续」，一次运行看全整个清单，便于逐项核对检测覆盖面。

**练习 3**：若把 test-wrong 用 `USE_STD_MALLOC=1` 编译并在 ASAN 下运行，行为有何不同？
**答案**：所有经 `mi(...)` 宏展开的调用改为系统 `malloc`/`free`，ASAN 原生拦截即可检测，不需要 mimalloc 适配层；但 L61-L62 两行没走宏，仍是 `mi_malloc_aligned`/`mi_free`，这部分依旧依赖 mimalloc 自己的追踪适配（若链接的是追踪构建的库）。

### 4.4 基准方法论：场景、强项与设计根源

#### 4.4.1 概念说明

readme 的 [Performance 一节](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L665-L697) 是理解「mimalloc 到底强在哪」的权威材料。它先立规矩（不存在全能算法、目标是「广谱不翻车」），再给一套覆盖真实程序到极端合成场景的基准，并配上少见的诚实免责声明。本模块的目标不是背数字，而是**把每个基准场景映射到我们已经学过的设计机制**，并掌握自己做 A/B 对比的正确姿势。

#### 4.4.2 核心流程

基准方法论的骨架：

- 对比对象：tcmalloc（Chrome）、jemalloc（Firefox/FreeBSD）、tbb、rpmalloc、Hoard、Mesh、glibc ptmalloc（[readme.md:L708-L716](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L708-L716)）。
- 计分方式：10 次运行取平均，结果以**相对 mimalloc 的耗时倍数**报告（1.2 = 比 mimalloc 慢 1.2 倍）；名字以 `N` 结尾的基准在**全部逻辑核上并行**运行（[readme.md:L721-L726](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L721-L726)）。
- 两个维度：耗时（上图）与峰值驻留内存 rss（[readme.md:L817-L827](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L817-L827)）——快但吃内存的分配器不是好分配器。
- 免责声明（[readme.md:L682-L691](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L682-L691)）：合成场景未必代表你的负载（连 jemalloc/tcmalloc 都在 xmalloc-testN 上翻车）；基准也没有覆盖长时运行服务与最坏延迟——mimalloc 为此做的很多优化（如减少长时服务的虚拟内存碎片）在榜单上根本体现不出来。

#### 4.4.3 源码精读

逐个场景读 readme 的定性描述，并挂靠已学机制：

| 基准 | 场景 | 压力点 | 对应的 mimalloc 机制（已学） |
| --- | --- | --- | --- |
| `cfrac` | 单线程、大量小而短命的分配（连分数分解） | 快路径裸吞吐 | u4-l1：直查数组 + 弹块，约 7 条指令 |
| `leanN` | Lean 定理证明器编译自带标准库，真实大规模并发负载 | 局部性对**整个程序**的外溢 | [readme.md:L734-L744](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L734-L744)：端到端 13% 加速，作者推断源于分配局部性改善了其他计算 |
| `redis` | 单线程常规服务负载 | 基本盘 | 各家都做得好（[readme.md:L746](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L746)） |
| `larsonN` | 跨线程分配再释放（server 的 bleeding 行为） | 对象跨线程迁移 | u5-l2：跨线程 free 仅一次 CAS；[readme.md:L748-L750](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L748-L750) |
| `mstressN` | 迁移 + 线程反复生灭、对象跨线程存活 | 线程退出后的页面归宿 | u6-l4：abandon/reclaim；[readme.md:L752-L756](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L752-L756) |
| `rptestN` | rpmalloc 系的多线程真实模式模拟 | 综合并发 | [readme.md:L758-L761](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L758-L761) |
| `alloc-test` | 百万级高强度多尺寸分配 | 线性扩展度标尺 | 单线程与 N 线程耗时一致 = 线性扩展（[readme.md:L766-L770](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L766-L770)） |
| `sh6bench` | SmartHeap 系，「反向」free 模式 | 释放顺序局部性 | u3-l2：free list 头插/收割的访问模式；mimalloc 比 jemalloc 快 2.5 倍以上（[readme.md:L772-L777](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L772-L777)） |
| `sh8bench` | sh6 + 对象跨线程迁移 | 迁移叠加释放模式 | u8-l2：multi-sharding；tcmalloc 因此慢 10 倍，mimalloc 稳住（[readme.md:L778-L779](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L778-L779)） |
| `xmalloc-testN` | **非对称**负载：一些线程只分配、另一些只释放 | 跨线程 free 的争用 | u8-l2：分片 thread free list 把 CAS 碰撞稀释到全堆，大幅领先（[readme.md:L781-L785](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L781-L785)） |
| `cache-scratch` | Hoard 引入的**被动伪共享**测试 | 缓存行在核间弹跳 | u3-l1/u8-l2：页与链表按线程私有分片，天然减少伪共享（[readme.md:L787-L797](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L787-L797)） |

这张表就是整本手册的「变现」：readme 里每一句定性优势，都能在 u3-u8 的某段源码里找到机制根源。

**test-stress.c 的正确定位**。仓库自带的多线程压力测试不是基准：其头部注释写明它模拟真实负载特征（尺寸按 2 的幂分布、分配时初始化并在释放时读回校验、指针跨线程交换、线程反复生灭），但最后一行是斩钉截铁的 **「Do not use this test as a benchmark!」**——因为线程调度是随机的（[test-stress.c:L7-L17](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L7-L17)）。它的价值在正确性：每个块写入 `(items-i)^cookie` 的校验模式，释放时逐字核对，任何内存损坏立即 `abort`（[test-stress.c:L173-L185](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L173-L185)）；跨线程交换靠 1000 槽的原子交换缓冲（[test-stress.c:L236-L243](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L236-L243)）。参数可从命令行覆盖 `THREADS/SCALE/ITER`（[test-stress.c:L413-L428](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L413-L428)）。想跑真正的基准，readme 指向独立的 [mimalloc-bench](https://github.com/daanx/mimalloc-bench) 仓库（[readme.md:L693-L697](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L693-L697)）。

#### 4.4.4 代码实践（本讲核心实践之下半）

**实践目标**：独立完成一次「系统 malloc vs mimalloc」的 A/B 耗时对比（这正是 u2-l1 的 `LD_PRELOAD` 技能的用武之地）。

**操作步骤**：

1. 构建 release 版 mimalloc（u1-l2），得到 `libmimalloc.so`（**不得**带 `-asan`/`-valgrind` 后缀，理由见练习 4.2-3）。
2. 选一个分配密集的真实程序。没有现成候选时，可自写一个：多线程各自做「分配 → 初始化 → 随机跨线程交换 → 释放」的循环（可参照 test-stress.c 的负载形状，但注意它自己声明的非基准属性，仅作负载生成器）。
3. 每组至少跑 5 次，取中位数：
   ```bash
   # A 组：系统 malloc
   for i in 1 2 3 4 5; do /usr/bin/time -v ./myprogram 2>&1 | grep -E 'Elapsed|Maximum resident'; done
   # B 组：LD_PRELOAD mimalloc（先用 MIMALLOC_VERBOSE=1 确认覆盖生效，再去掉再计时）
   for i in 1 2 3 4 5; do /usr/bin/time -v env LD_PRELOAD=$PWD/out/release/libmimalloc.so ./myprogram 2>&1 | grep -E 'Elapsed|Maximum resident'; done
   ```
4. 同时记录峰值 RSS（`Maximum resident set size`），别只看耗时。

**需要观察的现象**：B 组首行 verbose 出现 mimalloc 版本横幅（覆盖生效的证据，u2-l1）；两组的耗时中位数与峰值 RSS。

**预期结果**：分配密集且含跨线程交接的负载下 B 组应有可观提速；纯计算型程序则差异在噪声内。把配置（机器、核数、编译器、线程数）、数据、结论、局限（未控温、未绑核、样本量小）写成 200-300 字报告。**待本地验证**：具体数字以实测为准，不做任何预设。

#### 4.4.5 小练习与答案

**练习 1**：基准名以 `N` 结尾是什么意思？结果如何报告？
**答案**：在全部逻辑核上并行运行（如 32 逻辑核）；结果报告为相对 mimalloc 的耗时倍数，10 次平均，1.2 表示比 mimalloc 慢 1.2 倍（[readme.md:L721-L723](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L721-L723)）。

**练习 2**：为什么 mimalloc 在 `xmalloc-testN` 上「以很大幅度领先」，而很多工业级分配器在这里翻车？
**答案**：该基准的非对称模式（部分线程只分配、部分只释放）把所有释放压力打到跨线程路径上。mimalloc 的 multi-sharding（u8-l2）让每次跨线程 free 只是落在**页私有**字段上的一次 CAS，碰撞概率被稀释到全堆；而共享集中式 free list 的设计在这里会产生持续争用。

**练习 3**：test-stress.c 为什么不能当基准用？它的真正用途是什么？
**答案**：源码注释明说执行依赖（随机的）线程调度，结果不可复现（[test-stress.c:L14-L16](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L14-L16)）。它是正确性压力测试：靠 cookie 校验模式抓内存损坏、靠跨线程交换与线程生灭覆盖 abandon/reclaim 等高风险路径。

### 4.5 集成与发布：find_package 与 vcpkg

#### 4.5.1 概念说明

工具链的最后一环是「别人怎么用上这个库」。mimalloc 的发布形态有三：系统级安装后由 cmake 的 `find_package` 发现；包管理器（vcpkg）分发；以及 u1-l2 讲过的 `mimalloc.o` 单文件直链。本模块把官方推荐的 `find_package` 路径走通。

#### 4.5.2 核心流程

```text
安装 (cmake --install)
  → 生成 mimalloc 配置文件供 find_package 使用
  → 下游 CMakeLists: find_package(mimalloc 1.8 REQUIRED)
  → target_link_libraries(myapp PUBLIC mimalloc)          # 动态库
     或 target_link_libraries(myapp PUBLIC mimalloc-static) # 静态库
```

#### 4.5.3 源码精读

- [readme.md:L268-L280](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L268-L280) —— 官方推荐用法：`find_package(mimalloc 1.8 REQUIRED)`，再链 `mimalloc`（共享）或 `mimalloc-static`（静态）目标，并明说 `test\CMakeLists.txt` 就是现成示例（u1-l2 已确认它是安装后的官方示例工程，演示四种链接与覆盖方式）。
- [readme.md:L260-L267](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L260-L267) —— 「首选用法」段落：包含 `<mimalloc.h>`、链库、只用 `mi_` API；并强调 mimalloc 只用安全的 OS 调用（`mmap`/`VirtualAlloc`），可以和其他分配器在同一进程共存——这是它能被渐进式引入的前提。
- vcpkg 分发：readme 的发布说明里记录了「Add vcpkg portfile」与「Upstream `vcpkg` patches」（[readme.md:L934](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L934)、[readme.md:L945](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L945)），即官方在 vcpkg 生态维护移植清单；通过 vcpkg 安装后同样走 `find_package`。

#### 4.5.4 代码实践

**实践目标**：走通 `find_package` 集成路径（编写型实践）。

**操作步骤**：

1. 先安装本仓库构建产物：在 out/release 目录执行 `cmake --install .`（默认前缀可用 `-DCMAKE_INSTALL_PREFIX=$HOME/.local` 控制）。
2. 新建一个独立小工程，`CMakeLists.txt` 写入：
   ```cmake
   cmake_minimum_required(VERSION 3.18)
   project(mi-demo C)
   find_package(mimalloc 1.8 REQUIRED)   # 版本号可按需调低
   add_executable(mi-demo main.c)
   target_link_libraries(mi-demo PRIVATE mimalloc)
   ```
   `main.c` 里 `#include <mimalloc.h>`，`mi_malloc`/`mi_free` 一对调用，末尾 `mi_stats_print(NULL)`。
3. `cmake -B build && cmake --build build && ./build/mi-demo`。

**需要观察的现象**：配置阶段成功找到 mimalloc；程序结尾打印 blocks/pages/arenas/process 四段统计（u9-l3 讲过的报表）。

**预期结果**：不手写任何 `-I`/`-L` 路径即可编译链接运行。若 `find_package` 找不到，检查安装前缀是否在 `CMAKE_PREFIX_PATH` 里。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`mimalloc` 与 `mimalloc-static` 两个 link target 的差别是什么？
**答案**：前者链共享库（`libmimalloc.so`，部署时需随行），后者链静态库（`libmimalloc.a`，进可执行文件，无运行时依赖；对应的符号抢占式覆盖需要额外条件，见 u1-l2 的 `mimalloc.o` 讨论）。

**练习 2**：为什么 readme 强调 mimalloc「可以和其他分配器共存于同一进程」很重要？
**答案**：这允许渐进式引入：先在个别模块用 `mi_` API（甚至只给个别 STL 容器换 `mi_stl_allocator`，u2-l2），验证收益后再做全进程 `LD_PRELOAD` 覆盖；共存的前提是它只用 `mmap`/`VirtualAlloc` 这类安全 OS 接口拿内存，不 hack 系统分配器的内部结构。

## 5. 综合实践

把本讲全部内容串成一份**完整的评测与验收报告**（即本讲规格的代码实践任务）：

**阶段 A：检测能力矩阵（对应 4.1-4.3）**

1. 构建两份库：普通版（`out/release` 与 `out/debug`）与 ASAN 版（`out/asan`，`-DMI_TRACK=ASAN`）。可选再加 valgrind 版（`-DMI_TRACK=VALGRIND`）。
2. 用 4.3.4 的命令在两种（或三种）构建下分别运行 `test-wrong`。
3. 产出一张「错误类型 × 构建方式」的矩阵表：每格填「是否捕获 / 在哪一步捕获 / 报告内容摘要」。错误类型取 4.3.2 表格的九类；构建方式为 release（预期全漏）、debug（预期 free 时刻抓住写类与 double free）、ASAN（预期访问时刻抓住除「读未初始化」外的全部）、valgrind（预期最全，含未初始化读）。
4. 用 `MIMALLOC_VERBOSE=1` 的 `mem tracking:` 行和库名后缀固定你的构建证据。

**阶段 B：性能 A/B 对比（对应 4.4）**

按 4.4.4 的流程对真实程序做「系统 malloc vs `LD_PRELOAD` mimalloc」对比，记录耗时中位数与峰值 RSS，写 200-300 字短报告，须包含：测试场景与线程数、机器与编译环境、数据表、一句结论、一条局限。

**验收标准**：阶段 A 的矩阵能对应到 4.3.2 的原理分析（不一致处给出解释，例如 ASAN 为何不报未初始化读）；阶段 B 报告含覆盖生效证据（verbose 横幅）且明确指出用的是不带追踪后缀的 release 库。

## 6. 本讲小结

- track.h 用**一组宏合同**（分配记账 `mi_track_malloc_size`/`mi_track_free_size` + 内存三态 `defined`/`undefined`/`noaccess`）把块的生死与状态申报给外部工具；后端四选一：valgrind（三态完整）、ASAN（只有两态，`undefined` 降级为可访问）、ETW（事件画像，非错误检测）、none（空宏零开销）。
- **尺寸合同**：free 申报的区间必须与 malloc 申报的严格一致；`MI_PADDING` 开启时字节精确，关闭时存在块尾取整缝隙盲区。
- 追踪构建**改变了代码形态**而非仅添加宏：malloc 别名覆盖退化为函数转发、debug 魔数填充关闭、`rep movsb` 内联汇编关闭、部分覆盖测试目标被跳过——追踪构建的性能数据一律无效。
- test-wrong.c 是九类故意错误的**验收尺**：影子内存类工具在**访问时刻**抓错（连读越界都能抓），padding canary 在 **free 时刻**抓写穿——两张网互补，泄漏则靠退出时对账。
- readme 基准的每个场景都能映射回已学机制：`xmalloc-testN` ↔ 一次 CAS 的分片 thread free list、`larsonN`/`sh8bench` ↔ 跨线程迁移、`mstressN` ↔ abandon/reclaim、`leanN` ↔ 分配局部性的外溢收益；而 test-stress.c 是正确性压力测试、**明确禁止**当基准用。
- 集成发布走 `find_package(mimalloc)` + `mimalloc`/`mimalloc-static` 目标，vcpkg 官方维护 portfile；做性能对比必须用不带追踪后缀的 release 构建 + `LD_PRELOAD`，并同时报告耗时与峰值 RSS。

## 7. 下一步学习建议

本讲是手册最后一讲。至此你已从「mimalloc 是什么」一路读到「如何验证与评估它」。建议的后续路径：

1. **通读技术报告**：readme 多次指向 [Mimalloc: Free List Sharding in Action](https://www.microsoft.com/en-us/research/publication/mimalloc-free-list-sharding-in-action)，把 u8-l2 的源码级理解对齐到论文级的基准与论证。
2. **跑真正的基准**：克隆 [mimalloc-bench](https://github.com/daanx/mimalloc-bench)，在本地复现 readme 图表中的几个关键场景（`xmalloc-testN`、`larsonN`、`leanN`），并与你的综合实践 B 阶段结果互相印证。
3. **重走一遍源码**：以 `src/init.c` 的 `mi_process_init` 为起点，沿 `alloc.c → page.c → arena.c → prim` 把整条链路重读一遍——此时每个文件你都应该能说出「它在哪一讲出现过、解决什么问题」。
4. **做一次真实集成**：挑你自己的一个分配密集项目，按 4.5 的 `find_package` 路径（或 `LD_PRELOAD`）接入 mimalloc，用 u9-l3 的 `mi_stats_get_json` 建立内存基线，跑一轮 CI 回归——这是把这本手册变成生产力的最后一步。
