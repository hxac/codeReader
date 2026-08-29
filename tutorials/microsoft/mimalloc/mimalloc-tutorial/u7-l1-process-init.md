# 初始化流程：从第一次 mi_malloc 到 mi_process_done

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「谁来初始化 mimalloc」：进程加载器通过 constructor 属性在 `main` 之前触发 `_mi_auto_process_init`，而首次 `mi_malloc` 也能通过慢路径触发同样的初始化，两条路共用同一个 do-once 门闩。
2. 逐条列出 `mi_process_init_once` 的初始化步骤，并解释每一步为什么排在这个位置（选项 → 统计 → OS → 主堆 → page map → 线程 → TLS）。
3. 解释主堆、tld、theap 三者的创建顺序，以及为什么主线程的 tld/theap 是静态预分配的，而其他线程的要靠 `theap_meta` 元数据堆分配。
4. 描述线程退出（`_mi_thread_done`）与进程退出（`mi_process_done_once`）时分别发生什么：abandon、统计合并、打印时机。

## 2. 前置知识

本讲假设你已读过单元三（数据结构）和单元四、五（分配与释放链路）。先用三句话回顾要用的概念：

- **五层所有权**：subproc（子进程域）→ heap（一等堆）→ theap（线程本地堆，v3 新增）→ page queue → page → block。分配发生在 theap 上，heap 只是跨线程的「身份与账本」。
- **tld（thread local data，`mi_tld_t`）**：theap 背后的线程级簿记，记录线程 id、NUMA 节点、本线程拥有的 theaps 链表。
- **快路径直查数组**：小对象分配以 wsize 为下标查 `theap->pages_free_direct`，条目永不为 NULL——空队列时指向静态空页 `mi_page_empty`，于是「线程第一次分配」不需要任何特判，自然落入慢路径。这是初始化能被「顺便触发」的前提。

再补充两个本讲新引入的底层概念：

- **constructor / destructor 属性**：GCC/Clang 的 `__attribute__((constructor))` 把一个函数登记进 ELF 的 `.init_array` 段，动态加载器在跳转到 `main` 之前依次调用它们；`__attribute__((destructor))` 登记进 `.fini_array`，在进程退出阶段调用。这就是「代码还没跑，库先活过来」的机制。
- **do-once（call-once）模式**：多个线程、多个触发点可能同时到达初始化入口。`mi_atomic_do_once` 宏保证「恰好有一个执行者真正执行函数体，其余人阻塞等待完成」，效果类似 `pthread_once`，但不依赖 pthreads（mimalloc 自举期不能依赖任何运行时）。

最后是理解本讲的钥匙——**自举（bootstrapping）难题**：分配器初始化自己时可能需要分配内存（比如给新线程分配 tld），而此时分配器还没就绪。mimalloc 的解法是把「首批关键对象」全部做成静态变量（零成本、加载即就绪），其中还包括一个永不归属任何线程的元数据堆 `theap_meta`。看懂了这个套娃如何解套，就看懂了整个 init.c 的布局。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [src/init.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c) | 初始化本体：静态自举对象、`mi_process_init_once`、线程生命周期、`mi_process_done_once` |
| [src/options.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c) | 选项子系统在初始化时间线中的位置（语义细节已在 u2-l3 讲过，本讲只看时序） |
| [src/stats.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c) | `_mi_stats_init` 启动计时器；退出时 `mi_subproc_stats_print_out` 的打印路径 |
| [src/prim/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim.c) | 平台无关的 constructor/destructor 挂钩 |
| [src/prim/unix/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c) | pthread key：线程退出时自动回调 `_mi_thread_done` |
| src/subproc.c、src/os.c、src/page-map.c、src/arena.c、src/page.c | 时间线上各步骤最终落入的子系统，本讲只引用其入口函数 |

## 4. 核心概念与源码讲解

### 4.1 触发机制与静态自举：constructor、do-once 与空对象

#### 4.1.1 概念说明

mimalloc 的初始化有**两个互相独立的触发口**：

1. **加载器触发（正常路径）**：库被加载（动态库 preload、静态链接进可执行文件）时，constructor 函数在 `main` 之前运行，完成全部初始化。
2. **首次分配触发（兜底路径）**：如果 constructor 没来得及跑（比如某些平台的加载顺序问题），任何线程的第一次 `mi_malloc` 落入慢路径后，也会走到同一个初始化入口。

两条路必须**合流且只执行一次**——这就是 do-once 门闩的作用。

而初始化的「原料」是一组静态对象。为什么？看 init.c 里的一段注释：

> on some platforms lock_init or just a thread local access can cause allocation and induce recursion during initialization

即：某些平台上（如 macOS ≤14 的加载器会按需分配线程局部存储）连「访问一个 TLS 变量」都可能引发 malloc。如果初始化过程依赖堆分配，就会在分配器就绪之前递归调用分配器。解法是让最前面的一层完全不分配：

- `_mi_theap_empty`：静态空 theap，是所有线程 TLS 的**初始值**；
- `mi_tld_detached`：一个「无线程」的 tld，挂在 `_mi_theap_empty` 下面；
- `mi_process_heap_main` / `mi_process_theap_meta` / `mi_process_tld_main` / `mi_process_theap_main`：主 subproc 的主堆、元数据堆、主线程的 tld 与 theap，全部静态预分配。

#### 4.1.2 核心流程

```text
进程加载（LD_PRELOAD / 静态链接）
   │
   ▼
.init_array 中的 mi_process_attach        ← constructor 属性
   │
   ▼
_mi_auto_process_init()                   ← init.c
   ├── os_preloading = false              （此后可安全使用 C 运行时）
   ├── mi_process_init()                  ← do-once 门闩
   │      └── mi_process_init_once()      ← 真正的初始化步骤（见 4.2）
   ├── mi_process_setup_auto_thread_done()  ← do-once：注册线程退出钩子
   └── _mi_options_post_init()            ← 此时才安全打印：刷出延迟输出、打印选项表

main 运行……进程退出
   │
   ▼
.fini_array 中的 mi_process_detach        ← destructor 属性
   └── _mi_auto_process_done() → mi_process_done()（见 4.5）
```

首次分配的兜底路径（与上面合流于 `mi_process_init` 的 do-once）：

```text
mi_malloc(16)
  → _mi_theap_default()            // TLS，初始值是 &_mi_theap_empty
  → pages_free_direct[wsize]       // 指向静态空页 mi_page_empty
  → page->free == NULL             // 快路径自然弹空，无需特判
  → mi_theap_malloc_generic → _mi_malloc_generic        // page.c 慢路径
  → mi_malloc_generic_fallback → mi_malloc_generic_admin
  → 发现 theap 未初始化 → _mi_thread_init()
  → _mi_thread_init_with_heap(NULL)
       ├── mi_process_init()       ← 同一个 do-once，若已初始化则直接通过
       └── 创建本线程 tld + theap（见 4.4）
```

#### 4.1.3 源码精读

**constructor 挂钩**。GCC/Clang 下用 `__attribute__((constructor))`（Clang 还指定优先级 101，尽量排在普通 constructor 之前）把 `mi_process_attach` 登记进 `.init_array`：

- [src/prim/prim.c:L30-L46](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim.c#L30-L46)：定义 constructor `mi_process_attach` 与 destructor `mi_process_detach`，分别调用 `_mi_auto_process_init` / `_mi_auto_process_done`。注意这条通用路径只在平台没有提供更专门的方式（`MI_PRIM_HAS_PROCESS_ATTACH` 未定义，如 Windows 的 DllMain）时编译。

**do-once 门闩**。它是一个 `for` 循环宏：进入者调用 `_mi_atomic_once_enter` 抢执行权，抢到的执行循环体，退出时调用 `_mi_atomic_once_release` 放行其他等待者：

- [include/mimalloc/atomic.h:L555-L557](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L555-L557)：`mi_atomic_do_once` 宏定义。每个使用处都会展开出一个函数内静态的 `_mi_once` 状态量，所以「只执行一次」的粒度是**每个调用点各自独立**的。

**TLS 的初始值指向静态空 theap**。Linux 默认 TLS 模型（`MI_TLS_MODEL_LOCAL`）下，默认 theap 就是一个普通 thread-local 指针，初始值被写成空 theap 的地址：

- [src/prim/prim-tls.c:L27](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L27)：`__mi_theap_default = (mi_theap_t*)&_mi_theap_empty;`——新线程出生时它的默认 theap 就是这个空对象，`mi_theap_is_initialized` 判定为假，一切分配请求都会滑入慢路径。
- [include/mimalloc/internal.h:L640-L642](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L640-L642)：`mi_theap_is_initialized` 的判定依据是「theap 是否挂着一个 heap」——空 theap 的 heap 字段是 NULL。

**静态自举对象群**：

- [src/init.c:L120-L145](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L120-L145)：`_mi_theap_empty` 的静态初始化——直查小页数组全部填 `mi_page_empty`、tld 指向 `mi_tld_detached`、75 条页队列的 block_size 标签（`MI_PAGE_QUEUES_EMPTY` 宏在 [src/init.c:L66-L80](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L66-L80) 展开，正是 u3-l3 见过的那张尺寸表）。注意 `is_detached = true`、`refcount = 1`。
- [src/init.c:L108-L118](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L108-L118)：`mi_tld_detached`，线程 id 是哨兵值 `MI_THREADID_DETACHED`。
- [src/init.c:L151-L160](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L151-L160)：五个进程级静态对象（主堆、元数据 theap、主线程 tld/theap）与 `_mi_process_is_initialized` 标志。注释点明主线程的 tld/theap 预分配「不是严格必需，但对统计友好」——真正必需的是 `theap_meta`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「初始化代码排在 main 之前」以及「首次分配也能触发初始化」。

**操作步骤**（源码观察型，不需要运行程序）：

1. 构建一次 release 版库（如 u1-l2 所述 `mkdir -p out/release && cd out/release && cmake ../.. && make`，产物在 `out/release/libmimalloc.a` 或 `.so`）。
2. 对静态库执行 `nm -A out/release/libmimalloc.a | grep mi_process_attach`，确认该符号存在。
3. 对可用的对象文件执行 `objdump -s -j .init_array`（或 `readelf -x .init_array`）查看 `.init_array` 段内容，里面应有指向 `mi_process_attach` 的函数指针。
4. 对照 [src/prim/prim.c:L41-L46](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim.c#L41-L46)，理解这个指针就是加载器在 `main` 前调用的东西。

**需要观察的现象**：`.init_array` 段里能找到 mimalloc 登记的入口；静态库中 `mi_process_attach` 是 local 符号（`t` 类型）。

**预期结果**：符号与段都存在。若你的工具链输出格式不同（如 macOS 的 Mach-O 用 `otool -l` 查 `__mod_init_func` 段），属正常差异。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mi_atomic_do_once` 展开出的 `_mi_once` 是「每个调用点一个」而不是全局一个？

**参考答案**：该宏在函数体内声明 `static mi_atomic_once_t _mi_once`，静态局部变量的作用域是所在函数。`mi_process_init`、`mi_heap_main_init`、`mi_process_setup_auto_thread_done`、`_mi_page_map_init` 各有自己的门闩：它们常常嵌套调用（`mi_process_init_once` 内部会调 `mi_heap_main_init`），如果共用一个全局门闩，内层调用会和外层互相死锁/互相吞掉执行权。

**练习 2**：如果删除 constructor（假设平台不支持），程序还能正常用 `mi_malloc` 吗？

**参考答案**：能。首次 `mi_malloc` 的慢路径会经 `mi_malloc_generic_admin`（[src/page.c:L1011-L1021](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1011-L1021)）进入 `_mi_thread_init`，其第一步就是 `mi_process_init()`（[src/init.c:L309](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L309)）。constructor 的价值是让初始化**更早、更确定**地发生（并能在打印安全后输出选项表），而不是唯一入口。

**练习 3**：`_mi_theap_empty` 的 `refcount` 初始为 1、`is_detached` 为 true，这暗示了什么规则？

**参考答案**：空 theap 是永久存活的静态对象，永不被释放、永不归属任何具体线程（detached）。任何「归还默认 theap 引用」的代码对它做 decref 时计数永远不会降到触发释放的阈值，这保证了错误路径（比如对未初始化线程的访问）永远安全。

### 4.2 mi_process_init_once：进程初始化时间线

#### 4.2.1 概念说明

`mi_process_init_once` 是整个分配器的「开机程序」。它的步骤顺序不是随便排的，而是被三类约束咬合在一起：

1. **先有配置，后有动作**：后续所有子系统（OS 内存、arena、大页预留）都要读选项，所以 `_mi_options_init` 必须尽量靠前。
2. **先有账本，后有生意**：`mi_heap_main_init` 的注释明确写着 "before page_map_init so stats are working"——page map 初始化本身要分配内存、要记账，统计系统必须先能工作。
3. **能推迟的都推迟**：arena 的常规保留**不在**这里发生，而是等第一次真正需要页时惰性创建（这一点纠正一个常见误解：时间线里没有独立的「arena 保留」步骤，除非显式设置了预留选项）。

#### 4.2.2 核心流程

`mi_process_init_once` 的完整时间线（行号见 4.2.3）：

| 序 | 调用 | 作用 | 为什么在这个位置 |
| --- | --- | --- | --- |
| 1 | `_mi_verbose_message("process init: ...")` | 打印进程初始化日志 | 此时选项尚未批量初始化，本次调用会**顺手惰性初始化 verbose 这一个选项** |
| 2 | `_mi_detect_cpu_features()` | 探测 CPU 能力（如 BMI 指令） | 后续位图等模块按能力选实现 |
| 3 | `_mi_options_init()` | **批量初始化全部选项**（读环境变量） | 后续一切行为依赖配置 |
| 4 | `_mi_stats_init()` | 启动统计计时器（记录进程起点时刻） | 越早启动，统计越完整 |
| 5 | `_mi_os_init()` | prim 探测 OS 内存配置（页大小、地址空间位数等能力表） | 主堆初始化可能向 OS 要内存 |
| 6 | `mi_heap_main_init()` | 建 subproc 主域 + 主堆 + `theap_meta` 元数据堆 | 「先有账本」；page map 初始化需要能记账 |
| 7 | `_mi_page_map_init()` | 初始化指针→页的反查结构 | 之后创建的每个页都要登记 |
| 8 | `mi_thread_init()` | 初始化**当前线程**的 tld 与默认 theap，写入 TLS | 到这里本线程才能分配 |
| 9 | `_mi_tls_slots_init()` / `_mi_thread_locals_init()` | 创建 pthread key 等线程局部设施 | 注释说明这在 freeBSD 上可能分配，必须排在 thread_init 之后 |
| 10 | `_mi_process_is_initialized = true` | 置完成标志 | 之后 `mi_track_init`、可选的大页/内存预留 |
| 11 | `mi_track_init()` + 可选的 `mi_reserve_huge_os_pages_*` / `mi_reserve_os_memory` | 外部工具挂钩；按选项提前预留巨页/OS 内存 | 属于「配置驱动的附加动作」，放最后 |

注意两点：

- **arena 的惰性创建**：第 6 步并不预留 arena。第一个 arena 在第一次页分配时由 [src/arena.c:L525-L567](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L525-L567) 的 `mi_arenas_try_alloc` 创建：先在现有 arena 里找空闲 slice，找不到且未禁止 OS 分配时，在 `arena_reserve_lock` 保护下（双检：锁内再数一次 arena 数量）调用 `mi_arena_reserve` 保留新 arena，然后再找一次。
- **顺序敏感的自举细节**：第 8 步 `mi_thread_init` 内部会分配内存吗？主线程不会——它的 tld/theap 是静态的 `mi_process_tld_main` / `mi_process_theap_main`。其他线程才会走 `_mi_meta_zalloc`（见 4.4）。

#### 4.2.3 源码精读

**入口与门闩**：

- [src/init.c:L588-L592](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L588-L592)：`mi_process_init` 公共入口，一行 `mi_atomic_do_once { mi_process_init_once(); }`。注释说明调用者有三类：thread_init、进程加载器、首次分配。

**时间线主体**：

- [src/init.c:L537-L585](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L537-L585)：`mi_process_init_once` 全文。逐行对应上表的 11 步；L553-L554 的注释 "the following can potentially allocate (on freeBSD for pthread keys)" 解释了为什么 TLS 设施初始化排在 `mi_thread_init()` 之后——顺序反过来就会在自举期递归分配。
- [src/init.c:L566-L580](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L566-L580)：可选的巨页与 OS 内存预留，读 `mi_option_reserve_huge_os_pages` / `mi_option_reserve_os_memory`。这是唯一在 init 阶段「主动碰 arena/OS 大块内存」的代码，且由选项开关。

**加载器侧的封装** `_mi_auto_process_init`：

- [src/init.c:L506-L533](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L506-L533)：先把 `os_preloading` 置 false（此后可安全使用 C 运行时），再依次 `mi_process_init()` → `mi_process_setup_auto_thread_done()` → `_mi_options_post_init()`。`_mi_options_post_init` 里才调用 `mi_add_stderr_output` 把自举期攒在延迟缓冲区里的输出刷到 stderr，并在 verbose 时打印整张选项表——这就是你在 `main` 之前看到 mimalloc 横幅的原因。末尾还会对随机数做「若太弱则重播种」。

**被时间线调用的各子系统入口**（各一行，供对照）：

- [src/stats.c:L433-L438](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L433-L438)：`_mi_stats_init` 记录进程起始时刻，供统计输出里计算 elapsed 时间。
- [src/os.c:L99-L101](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L99-L101)：`_mi_os_init` 就是调 prim 的 `_mi_prim_mem_init` 填能力表 `mi_os_mem_config`（u6-l1 讲过的那张家）。
- [src/page-map.c:L359-L365](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L359-L365)：`_mi_page_map_init` 自己也套了一层 do-once（另一处独立门闩的例子）。
- [src/subproc.c:L316-L322](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L316-L322)：`_mi_subproc_main_init` 返回静态的 `mi_process_subproc_main`，同样零分配。

**主堆与元数据堆的建立**（时间线第 6 步的内部）：

- [src/init.c:L184-L208](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L184-L208)：`mi_heap_main_init_once`。顺序是：先 `_mi_subproc_main_init()` 建主 subproc → 初始化 detached tld → 用 release 原子写把 `mi_process_heap_main` 发布到 `subproc_main->heap_main` → `_mi_heap_init` 初始化主堆字段 → 初始化 `mi_process_theap_meta` 并挂到 `subproc_main->theap_meta`。两个安全相关的细节：`allow_page_abandon = false`（元数据页不与其他线程共享，防安全攻击面）、`page_full_retain = 2`。
- [src/init.c:L210-L214](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L210-L214)：`mi_heap_main_init` 的 do-once 包装。
- [src/init.c:L216-L230](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L216-L230)：`_mi_subproc_heap_main` 是获取主堆的常规入口：先 acquire 读 `subproc->heap_main`，非空直接返回；空且是主 subproc 才触发 `mi_heap_main_init()`——又一次「惰性 + do-once」。

#### 4.2.4 代码实践

**实践目标**：用 `MIMALLOC_VERBOSE=1` 的真实输出，对照 `mi_process_init_once` 的源码顺序，亲手排出初始化时间线（本讲的核心实践）。

**操作步骤**：

1. 写一个最小程序 `mi-init.c`（示例代码）：

   ```c
   #include <stdio.h>
   #include <mimalloc.h>

   int main(void) {
     printf("---- main starts ----\n");
     void* p = mi_malloc(16);
     printf("p = %p\n", p);
     mi_free(p);
     printf("---- main ends ----\n");
     return 0;
   }
   ```

2. 编译链接（示例命令，按你的构建产物调整）：

   ```bash
   gcc -I include -o mi-init mi-init.c out/release/libmimalloc.a -lpthread
   ```

3. 运行：

   ```bash
   MIMALLOC_VERBOSE=1 ./mi-init
   ```

4. 把输出逐行抄下来，在每行旁边标注它来自哪个源码调用点，然后回答：`process init` 消息、选项表、`main starts`、`process done` 四者的相对顺序是什么？

**需要观察的现象**：`process init: 0x...` 与整张选项表（以版本号 `v3.5.0...` 开头）出现在 `---- main starts ----` **之前**；`process done ...` 出现在 `---- main ends ----` **之后**。

**预期结果**：时间线为「process init（init.c:541）→ 选项表（`_mi_options_post_init` → `mi_options_print`）→ main → process done（init.c:646）」。特别注意：**输出里不会有独立的 arena 保留信息**——因为 arena 是首次页分配时惰性创建的（见 4.2.2）。完整的源码级时间线应为：

```text
constructor(mi_process_attach)
  → _mi_auto_process_init
    → mi_process_init_once
        1. verbose "process init"      ← 惰性初始化 verbose 选项
        2. _mi_detect_cpu_features
        3. _mi_options_init            ← 选项解析（环境变量）
        4. _mi_stats_init              ← 计时器启动
        5. _mi_os_init                 ← OS 能力表
        6. mi_heap_main_init           ← subproc + 主堆 + theap_meta
        7. _mi_page_map_init           ← 指针→页反查结构
        8. mi_thread_init              ← 主线程 tld/theap + TLS 绑定
        9. _mi_tls_slots_init 等
    → _mi_options_post_init            ← 打印选项表（main 前最后一件事）
main
  → 首次 mi_malloc → （已初始化，直接走快/慢路径）
     └─ 首个 arena 惰性保留（arena.c:mi_arenas_try_alloc）
退出
  → destructor → mi_process_done
```

具体打印内容随版本与构建类型略有差异，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `_mi_stats_init()` 挪到 `_mi_options_init()` 之前会怎样？

**参考答案**：基本能跑，但语义受损：`_mi_stats_init` 只在 `mi_process_start == 0` 时记录起始时刻（[src/stats.c:L436-L438](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L436-L438)），提前一点只会让 elapsed 统计略多算一点初始化时间。真正不能乱动的是第 6/7/8 步的相对顺序（账本→page map→线程）。

**练习 2**：为什么不把「保留第一个 arena」也放进 `mi_process_init_once`？

**参考答案**：进程加载期（constructor 阶段）很多程序根本不会用 mimalloc 分配多少内存；arena 保留是 1GiB 级的虚拟内存动作（u6-l3），提前做只增加 VMA 与记账负担。惰性到「第一次页分配」再做，天然与需求匹配；且 `mi_arena_reserve` 自带 `arena_reserve_lock` 双检，并发安全。

**练习 3**：`mi_heap_main_init_once` 里为什么必须先 `_mi_subproc_main_init()` 再初始化主堆？

**参考答案**：主堆 `_mi_heap_init(&mi_process_heap_main, ..., subproc_main, 0)` 的参数就需要 subproc 指针（[src/heap.c:L102-L114](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L102-L114) 里它被存进 `heap->subproc` 并用来给 `heap_total_count` 计数）。所有权方向决定创建顺序：subproc 是根，先有根才能挂堆。

### 4.3 选项初始化的时序视角：_mi_options_init 为何必须先行

#### 4.3.1 概念说明

u2-l3 已经讲过选项系统的语义：47 个选项、三态 init（UNINIT/DEFAULTED/INITIALIZED）、四种设置途径的优先级、`MIMALLOC_` 环境变量的解析规则。**本讲不重复这些**，只回答一个时序问题：`_mi_options_init` 在开机时间线里干了什么、为什么它必须排在 OS/arena/大页动作之前？

关键在于 mimalloc 的选项是**惰性逐个初始化**的：`mi_option_get(option)` 发现某选项还是 `MI_OPTION_UNINIT` 时才去读环境变量（[src/options.c:L275-L284](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L275-L284)）。`_mi_options_init` 做的事情就是**强行把每个选项都 get 一遍**，把整个表推到「已初始化」状态。为什么必须主动做这件事？两个原因：

1. 自举期（`os_preloading` 为 true 时）`getenv` 在某些平台不可用，`mi_option_init` 读失败会**保持 UNINIT 以便稍后重试**（[src/options.c:L692-L696](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L692-L696) 的注释）；在安全时点统一补一轮，保证后续热路径 `_mi_option_get_fast` 裸读的值是可靠的。
2. 带副作用的选项组合检查（如 MI_GUARDED 构建下强制关掉大页）需要在其他子系统读选项之前完成。

#### 4.3.2 核心流程

```text
mi_process_init_once
  └─ _mi_options_init                       // options.c:187
       ├─ for 每个选项 i: mi_option_get(i)  // 触发惰性 mi_option_init
       │    └─ mi_option_init(desc)         // options.c:623
       │         ├─ 拼 "mimalloc_<name>"（失败再试 legacy 名）
       │         ├─ getenv → 布尔词表 / strtol / KiB 后缀
       │         └─ 写 desc->value 与 desc->init
       ├─ 读 max_errors / max_warnings
       └─ （MI_GUARDED）若开了 guarded 采样则禁用大页并告警

随后（_mi_auto_process_init 末段）
  └─ _mi_options_post_init                  // options.c:206
       ├─ mi_add_stderr_output()            // 刷延迟缓冲、切换默认输出
       └─ verbose 时 mi_options_print()     // 打印版本+全部选项+构建配置
```

#### 4.3.3 源码精读

- [src/options.c:L187-L203](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L187-L203)：`_mi_options_init` 的强制遍历。注释 "called on process load"。
- [src/options.c:L623-L696](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L623-L696)：`mi_option_init` 单个选项的环境解析（u2-l3 精读过解析规则，此处关注 L692-L696 的「读不到环境先保持 UNINIT、以后重试」分支——这正是 `_mi_options_init` 必须存在的理由）。
- [src/options.c:L205-L209](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L205-L209)：`_mi_options_post_init`，注释 "called at actual process load, it should be safe to print now"。配合 [src/options.c:L431-L436](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L431-L436) 的 `mi_add_stderr_output`：把自举期积压在 `out_buf`（16KiB 延迟缓冲）里的输出刷到 stderr，并把默认输出切到「stderr+缓冲」双写。
- [src/options.c:L214-L260](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L214-L260)：`mi_options_print_out`，即 verbose 时你看到的那份「版本横幅 + option 逐行 + 构建配置」报表的生成处。
- [src/init.c:L541](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L541) 与 [src/init.c:L544](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L544) 的对照：第 541 行的 `_mi_verbose_message` 发生在 `_mi_options_init()` **之前**，它内部的 `mi_option_is_enabled(mi_option_verbose)` 会单独把 verbose 这一个选项惰性初始化（读一次环境变量）。也就是说：如果你用 `MIMALLOC_VERBOSE=1` 运行，这条消息能打出来，靠的正是惰性初始化这个兜底。

#### 4.3.4 代码实践

**实践目标**：验证「环境变量调参」与「程序内调参」在输出时序上的差异（承接 u2-l3 的结论，从初始化时序角度复核）。

**操作步骤**：

1. 复用 4.2.4 的 `mi-init.c`，在 `main` 最前面（`printf` 之前）加一行 `mi_option_enable(mi_option_verbose);`（示例代码修改）。
2. 不带任何环境变量重新编译运行，观察输出。
3. 再用 `MIMALLOC_VERBOSE=1 ./mi-init` 运行，对比两次输出。

**需要观察的现象**：程序内 enable 时，`process init` 等自举期消息**不会**出现（那时 verbose 还是 0），只有 main 之后的 verbose 输出；环境变量方式则完整打印自举期消息与选项表。

**预期结果**：与 u2-l3 的结论一致——`MIMALLOC_VERBOSE=1` 经由 `_mi_options_post_init`（main 前）打印选项表，而 `mi_option_enable` 发生在 main 之后，错过了自举期。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`_mi_option_get_fast`（热路径裸读）为什么敢不加锁、不检查 init 状态？

**参考答案**：因为 `_mi_options_init` 在进程加载时已把所有选项推到非 UNINIT 状态，之后 `desc->value` 只会被 `mi_option_set` 类接口按普通写修改（[src/options.c:L266-L272](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L266-L272)）。u2-l3 讲过的「初始化期数据竞争无害（大家都解出同一个值）」注释也点明了这一点。

**练习 2**：为什么 `_mi_options_post_init` 里才「安全打印」，自举期的输出去哪了？

**参考答案**：自举期（constructor 阶段）C 运行时的 stdio 可能未就绪，mimalloc 用 `out_buf` 延迟缓冲攒输出（[src/options.c:L361-L363](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L361-L363)），等 `mi_add_stderr_output` 时一次性刷出。所以 `process init` 消息虽然产生于最早期，呈现顺序仍正确。

### 4.4 线程生命周期：_mi_thread_init_with_heap 与 _mi_thread_done

#### 4.4.1 概念说明

进程初始化只解决「主线程」。任何其他线程（以及主线程本身，如果它绕过了 constructor）第一次碰 mimalloc 时，都要走 `_mi_thread_init_with_heap` 建立**本线程的** tld 和默认 theap。核心设计：

- **主线程零分配**：主 subproc 的第一个 tld 及其 theap 是静态对象（`mi_process_tld_main` / `mi_process_theap_main`），不发生任何堆分配。
- **其他线程靠元数据堆**：新线程的 tld 和 theap 用 `_mi_meta_zalloc` 从 `theap_meta` 分配——这个堆挂在 detached tld 上、不归属任何线程，所以「给线程建分配器」这件事本身不依赖该线程已初始化，也不会递归。
- **线程退出走 abandon，不走销毁**：线程结束时它的 theaps 不释放内存，而是把页面移交给 abandoned 机制（u6-l4），等别的线程认领。tld 本身则被释放。

#### 4.4.2 核心流程

```text
线程首次分配（或显式 mi_thread_init）
  → _mi_thread_init_with_heap(NULL)
      1. mi_process_init()                      // 保证进程已初始化（do-once 直接通过）
      2. 取 _mi_theap_default()；若已初始化则直接返回（幂等）
      3. heap_main = mi_heap_main()             // 主堆（惰性确保已建立）
      4.（debug）检查是否已有本线程的 theap —— 重入检测
      5. tld = mi_tld_create(subproc)
           - 主 subproc 且是第 0 个 → 静态 mi_process_tld_main
           - 否则 → _mi_meta_zalloc(theap_meta)
      6. theap =（主线程）静态 mi_process_theap_main
                （其他线程）_mi_theap_alloc(heap_main, tld)
      7. _mi_theap_init(theap, heap_main, tld)
      8. 先 _mi_theap_default_set(theap)        // 写 TLS：从此本线程有了默认 theap
         再 _mi_heap_theap_set(heap_main, theap) // 它会访问 TLS，必须后做
      9. 统计：subproc threads +1

线程退出（pthread key 析构 / DllMain / 显式 mi_thread_done）
  → _mi_thread_done
      1. 重入保护：默认 theap 未初始化则直接返回
      2. _mi_thread_locals_thread_done()        // 释放动态 thread_local
      3. 统计：threads -1；校验线程 id 匹配
      4. mi_thread_theaps_done(tld)
           - 对本线程每个 theap：_mi_theap_collect_abandon  // 页面遗弃
           - 默认 theap 复位回 _mi_theap_empty（TLS 写回空对象）
           - _mi_tld_detach_theaps：把 theaps 链从各 heap 上摘除
           - 逐个 decref 释放 theap 结构本身
      5. mi_tld_free(tld)                        // tid 置无效值、释放 tld
```

#### 4.4.3 源码精读

**线程初始化**：

- [src/init.c:L306-L361](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L306-L361)：`_mi_thread_init_with_heap` 全文。L316-L317 的注释解释了为什么第 3 步之前不能碰 thread-local（macOS 加载器会因 TLS 访问而递归分配）。L350-L352 是顺序关键：先 `_mi_theap_default_set`（本函数内只写一个 TLS 槽），后 `_mi_heap_theap_set`（内部要走通用 thread-local 机制）。
- [src/init.c:L254-L273](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L254-L273)：`mi_tld_create` 的分叉——`tseq==0`（主 subproc 第一个 tld，即主线程）用静态对象并配 `MI_MEM_STATIC` 产地证；否则 `_mi_meta_zalloc`。L255 的断言注释再次强调「theap_meta 必须在别的线程分配之前、由主线程初始化好」。
- [src/init.c:L236-L251](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L236-L251)：`mi_tld_init` 填基础字段：NUMA 节点、线程 id、是否线程池、序号，并把 subproc 的 `thread_count` 原子加一。
- [src/init.c:L363-L369](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L363-L369)：公开 API `mi_thread_init` 只是转调 `_mi_thread_init()`。

**触发点**（谁会调它）：除了显式调用，慢路径是自动触发点：

- [src/page.c:L1011-L1021](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1011-L1021)：`mi_malloc_generic_admin` 发现 theap 未初始化即调 `_mi_thread_init()`；若 theap 是 `_mi_theap_empty_wrong`（一等堆建 theap 失败的错误标记）则返回 NULL 走 OOM。
- [src/heap.c:L60-L69](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L60-L69)：在一等堆上首次分配也会先 `mi_thread_init()`（u7-l3 展开）。

**线程退出**：

- [src/init.c:L452-L481](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L452-L481)：`_mi_thread_done`。L473-L474 的线程 id 校验针对 Windows FLS 关闭顺序问题（退出线程可能替别的线程执行析构）。L468 先释放动态 thread_locals。
- [src/init.c:L378-L422](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L378-L422)：`mi_thread_theaps_done`。注意 L385-L386 的注释：**永不销毁 theap 结构所管辖的页面**——静态链接的 dll 场景下，`mi_fls_done` 之后仍可能有 free 调用进来（issue #207），abandon 机制保证这些指针仍可安全释放。L396-L397 把 TLS 的默认与缓存 theap 都写回 `_mi_theap_empty`。
- [src/init.c:L275-L282](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L275-L282)：`mi_tld_free`，把线程 id 置为 `~0`（OS 会复用线程 id，issue #1287）；`_mi_meta_free` 对静态 tld 安全（产地证是 `MI_MEM_STATIC` 时释放为空操作）。
- [src/prim/unix/prim.c:L1017-L1034](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L1017-L1034)：unix 平台的自动挂钩——`_mi_prim_thread_init_auto_done` 创建带析构回调 `mi_pthread_done` 的 pthread key（在 4.1 流程图的 `mi_process_setup_auto_thread_done` 中调用）；线程退出时 pthreads 自动调 `mi_pthread_done` → `_mi_thread_done`。进程退出侧的 `_mi_prim_thread_done_auto_done` 负责删除 key 防泄漏（issue #809）。

#### 4.4.4 代码实践

**实践目标**：观察「线程加入/退出」在统计上的体现，并跟踪一条线程析构调用链。

**操作步骤**（示例代码）：

1. 写程序 `mi-threads.c`：`main` 里 `mi_malloc` 一次，再创建 2 个 `pthread`，每个线程里 `mi_malloc`/`mi_free` 若干次后 `pthread_join`：

   ```c
   #include <stdio.h>
   #include <pthread.h>
   #include <mimalloc.h>

   static void* worker(void* arg) {
     for (int i = 0; i < 100; i++) { mi_free(mi_malloc(32)); }
     return NULL;
   }

   int main(void) {
     mi_free(mi_malloc(16));          // 主线程触发完整初始化
     pthread_t t1, t2;
     pthread_create(&t1, NULL, worker, NULL);
     pthread_create(&t2, NULL, worker, NULL);
     pthread_join(t1, NULL);
     pthread_join(t2, NULL);
     return 0;
   }
   ```

2. 用 `MIMALLOC_SHOW_STATS=1 ./mi-threads` 运行（建议用 debug 构建，统计更全），在输出的 process 段找 `threads` 一行。
3. 源码跟踪（不运行也行）：从 [src/prim/unix/prim.c:L1017-L1021](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L1017-L1021) 的 `mi_pthread_done` 出发，手工写出 worker 线程退出时到 `_mi_theap_collect_abandon` 的完整调用链。

**需要观察的现象**：`threads` 统计为 3（主线程 + 2 个 worker）；两个 worker 退出后页面进入 abandoned 状态而非立刻归还。

**预期结果**：统计行约为主 subproc 的 `threads: 3 peak ...`；调用链为 `mi_pthread_done → _mi_thread_done(NULL) → mi_thread_theaps_done(tld) → _mi_theap_collect_abandon(theap)`。abandoned 计数是否可见取决于构建与统计级别，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_mi_thread_init_with_heap` 里 `_mi_theap_default_set` 必须先于 `_mi_heap_theap_set`？

**参考答案**：源码注释（[src/init.c:L350-L352](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L350-L352)）写明：后者内部会访问 thread locals（通用的 `_mi_thread_local_set` 路径），而 TLS 访问在部分平台可能触发递归分配；递归分配进入 `mi_malloc` 时会先读 `_mi_theap_default()`——只有默认 theap 已设置好，递归才能安全落在已初始化的 theap 上而不是再次进入初始化。

**练习 2**：线程退出后，另一个线程 free 该线程分配的指针，为什么不会崩？

**参考答案**：`mi_thread_theaps_done` 不释放页面内存，只把页面移入 abandoned 状态（xthread_id 清零、登记进 arena 的 pages_abandoned 位图，见 u6-l4）；随后到来的跨线程 free 通过 page map 仍能反查出页，并经所有权位 CAS 认领该页。tld 释放（`mi_tld_free`）不影响这条路径，因为 free 快路径不查 TLS。

**练习 3**：主线程的 tld 是静态的，那 `mi_tld_free` 对它调用 `_mi_meta_free` 会不会出问题？

**参考答案**：不会。静态 tld 的 `memid` 是 `_mi_memid_create_static`（[src/init.c:L262](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L262)），`_mi_meta_free` 按产地证分支处理，静态产地不执行真正的释放——这正是 u3-l1 引入 mi_memid_t「产地证」概念的意义。

### 4.5 进程退出：mi_process_done_once 的统计与回收

#### 4.5.1 概念说明

`mi_process_done_once` 在进程退出阶段被 destructor（或 Windows 的 DllMain DLL_PROCESS_DETACH、atexit）触发。它要处理三件互相牵制的事：

1. **兜底回收**：尽可能把线程资源释放掉（让 `_mi_thread_done` 在所有非主线程上生效）。
2. **统计输出**：若开了 `show_stats`/`verbose`，把散落在各 theap/heap 的统计**合并**后打印。合并是必须的，因为统计平时按线程各自记账（避免原子争用），只有打印时才汇总。
3. **谨慎的内存归还**：退出后仍可能有代码调用 `free`（atexit 例程、C 运行时终止代码），所以默认**不**归还所有内存；只有显式开启 `destroy_on_exit` 才做全量销毁。

#### 4.5.2 核心流程

```text
destructor(mi_process_detach) → _mi_auto_process_done → mi_process_done (do-once)
  → mi_process_done_once
      0. 守卫：进程未初始化过 / 已 done 过 → 直接返回
      1. _mi_theap_cached_set(_mi_theap_empty)     // 缓存 theap 引用减掉
      2. _mi_prim_thread_done_auto_done()          // 删 pthread key
      3.（MI_DEBUG 或静态库构建）mi_theap_collect(默认 theap, force=true)
      4. mi_track_done()                            // 通知跟踪工具
      5. 分岔：
         a. destroy_on_exit 开启 → _mi_subprocs_unsafe_destroy_all
            （销毁全部 subproc、arena、thread locals、page map）
         b. 默认 → 释放动态 thread locals；
            若 show_stats/verbose → 合并 theap_meta 与默认 theap 的统计、
            heap 统计并入 subproc，最后 mi_subproc_stats_print_out 打印
      6. _mi_tls_slots_done / _mi_subproc_main_done / _mi_allocator_done
      7. verbose 打印 "process done <sizeof(mi_page_t)>"
      8. os_preloading = true                       // 回到「别再碰 C 运行时」状态
```

#### 4.5.3 源码精读

- [src/init.c:L596-L648](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L596-L648)：`mi_process_done_once` 全文，对应上面 0-8 步。
- [src/init.c:L610-L617](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L610-L617)：强制 collect 的条件编译——`MI_DEBUG` 或非共享库（静态链接）时执行。注释解释动机：独立进程不必归还（OS 反正会回收），但 mimalloc 被静态链进**会被反复加载/卸载的共享库**时必须归还（issue #281）。
- [src/init.c:L622-L641](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L622-L641)：退出路径的核心分岔。L622 的注释直言全量释放 "can be dangerous in general if overriding regular malloc/free"——因为 process_done 之后仍可能有 `free` 调用进来，而 page map 都已经没了。默认分支里 L634-L639 是统计合并顺序：`_mi_theap_merge_stats(theap_meta)` → `_mi_theap_merge_stats(_mi_theap_default())` → `mi_heap_stats_merge_to_subproc` → `mi_subproc_stats_print_out`。
- [src/init.c:L652-L662](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L652-L662)：`mi_process_done` 的 do-once 包装与 `_mi_auto_process_done`。后者的守卫读 `destroy_on_exit >= 2` 时直接返回——提供了一个「彻底关掉自动 done」的开关（配合选项即可防止危险的退出期行为）。
- [src/stats.c:L512-L523](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L512-L523)：`mi_subproc_stats_print_out` 聚合 subproc 统计并打印；公开的 `mi_stats_print_out` 只是转调它。你在退出时看到的四大段报表（blocks/pages/arenas/process）就从这里出来（u9-l3 展开）。
- [src/init.c:L646-L647](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L646-L647)：最后一行 verbose 输出 `process done %zu` 打的是 `sizeof(mi_page_t)`；随后 `os_preloading = true` 把模块拨回「预加载态」，之后任何再进来的输出/运行时调用都会被 4.3 提到的递归守卫拦下。

#### 4.5.4 代码实践

**实践目标**：确认统计打印发生在 `main` 返回之后，并观察 `destroy_on_exit` 的行为差异。

**操作步骤**：

1. 复用 4.4.4 的 `mi-threads`（或 4.2.4 的 `mi-init`），运行：

   ```bash
   MIMALLOC_SHOW_STATS=1 ./mi-threads
   ```

   确认报表出现在 `---- main ends ----`（如果你加了这行打印）之后。

2. 再运行一次加退出销毁：

   ```bash
   MIMALLOC_SHOW_STATS=1 MIMALLOC_DESTROY_ON_EXIT=1 ./mi-threads
   ```

3. 对照 [src/init.c:L626-L628](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L626-L628) 思考：为什么默认不这样做？

**需要观察的现象**：第一次运行正常打印统计；第二次运行可能不再打印统计（走 destroy 分支，L630-L640 的打印分支被跳过），程序正常退出。

**预期结果**：统计输出位于进程退出阶段（main 之后）；`destroy_on_exit=1` 时输出内容与默认分支不同。若程序在 destroy 模式下因退出后的悬空 free 崩溃，那正是注释警告的场景。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么统计打印前要专门 `_mi_theap_merge_stats(subproc_main->theap_meta)`？

**参考答案**：元数据堆 `theap_meta` 上的分配（各线程的 tld/theap 结构、page map 子表等）也是真实的内存开销，但挂在 detached tld 上、不属于任何常规 heap 的统计；不合并它，进程级报表就会漏掉这部分元数据开销。

**练习 2**：`_mi_auto_process_done` 里 `destroy_on_exit >= 2` 的守卫有什么用？

**参考答案**：`mi_option_destroy_on_exit` 有两层含义：值为 1 时开启退出全量销毁（L626）；值 ≥2 时**连同自动 done 本身一起关闭**（L660）——给极端场景（例如宿主程序自己管理 mimalloc 生命周期，或 destructor 阶段调用顺序不可控）一个彻底的逃生口。

**练习 3**：退出阶段最后为什么要把 `os_preloading` 置回 true？

**参考答案**：`os_preloading` 是「模块是否处于可用 C 运行时的状态」标志（[src/init.c:L493-L498](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L493-L498)）。退出收尾做完后，进程里可能还有别的 destructor 再跑并调用 `free`，此时模块必须回到最保守的「预加载」姿态，让输出与递归相关的守卫（如 [src/options.c:L452-L457](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L452-L457) 的 `mi_recurse_enter`）拦下不安全的动作，与加载前对称。

## 5. 综合实践

把本讲所有模块串成一个「初始化时间线考古」任务：

1. **准备**：按 u1-l2 分别构建 release 与 debug 两个版本，写好 4.2.4 的 `mi-init.c`。
2. **采集**：对两个版本分别运行：

   ```bash
   MIMALLOC_VERBOSE=1 MIMALLOC_SHOW_STATS=1 ./mi-init > verbose.txt 2>&1
   ```

3. **标注**：把 `verbose.txt` 的每一行输出映射回源码调用点（`process init` ← init.c:541；选项表 ← options.c:214；各 thread 消息 ← init.c 4.4 的路径；`process done` ← init.c:646；统计报表 ← stats.c:512）。
4. **画线**：在纸上排出完整时间线，并特别回答两个问题：
   - 「选项解析 → OS 初始化 → 主堆建立 → TLS 绑定」的相对顺序是什么？（对照 4.2.2 的表验证）
   - 「arena 保留」发生在哪一步？（正确答案：不在 init 阶段，而在首次页分配时惰性发生——用 `MIMALLOC_RESERVE_OS_MEMORY=65536 ./mi-init` 重跑对照：这次 arena 预留被显式提前到了 init 的第 11 步，[src/init.c:L575-L580](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L575-L580)。）
5. **进阶**（可选）：用 `gdb --args ./mi-init`，在 `mi_process_init_once`、`mi_heap_main_init_once`、`_mi_thread_init_with_heap`、`mi_process_done_once` 四处下断点，`run` 后逐个 `continue`，用 `bt` 确认每条路径的真实触发者（constructor 还是首次分配）。

**验收标准**：你能不看讲义说出——初始化的三个触发口、`mi_process_init_once` 的步骤顺序及理由、主线程与其他线程在 tld/theap 创建上的差异、退出时统计合并的顺序与「为何默认不归还全部内存」。

## 6. 本讲小结

- mimalloc 的初始化有**两个触发口**（加载器 constructor 与首次分配慢路径），靠**每调用点一个**的 `mi_atomic_do_once` 门闩合流为恰好一次的执行。
- 自举难题的解法是**静态对象群**：`_mi_theap_empty`（TLS 初始值）、`mi_tld_detached`、主 subproc/主堆/`theap_meta`/主线程 tld 与 theap 全部零分配预建；其他线程的 tld/theap 才从 `theap_meta` 元数据堆分配。
- `mi_process_init_once` 的顺序被三条约束咬合：**配置先行**（`_mi_options_init` 批量读环境）、**账本先行**（主堆先于 page map）、**能懒则懒**（arena 不在此阶段保留，首个 arena 由首次页分配触发 `mi_arena_reserve`）。
- 线程初始化的顺序关键是**先写默认 theap、再挂 heap↔theap 关系**（后者访问 TLS 可能递归分配）；线程退出**不释放页面**，只 abandon，tld 才真正释放。
- 进程退出默认**只统计不归还**：合并 theap_meta/默认 theap/heap 的统计后经 `mi_subproc_stats_print_out` 打印；只有显式 `destroy_on_exit` 才全量销毁，因为退出后仍可能有 `free` 进来。
- `os_preloading` 标志首尾对称：加载期置 false 允许用 C 运行时，退出收尾后置回 true，让递归守卫拦下此后的一切不安全调用。

## 7. 下一步学习建议

- **u7-l2 线程本地存储**：本讲多次出现「访问 TLS 可能引发递归分配」，下一讲深入 `prim-tls.h`/`prim-tls.c` 的多种 TLS 模型（局部动态、pthread、Windows slot）与 `_mi_theap_default_set`/`_mi_theap_cached_set` 的两级缓存，解释这句话在各平台上的具体细节。
- **u7-l3 一等堆**：本讲的 `_mi_thread_init_with_heap(heap_main)` 已经把「线程初始化」与「堆」解耦，下一讲看 `mi_heap_new/delete/destroy` 如何复用这套机制实现跨线程堆分配与整堆释放。
- 若想先横向扩展，可回读 [src/init.c:L378-L422](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L378-L422)（`mi_thread_theaps_done`）并对照 u6-l4 的 abandon 位图实现，把「线程死亡的内存去向」这条线彻底闭环。
