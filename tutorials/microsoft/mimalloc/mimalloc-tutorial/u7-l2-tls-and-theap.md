# 线程本地存储：默认 theap 的查找与缓存

## 1. 本讲目标

上一讲（u7-l1）我们看清了进程级初始化：`mi_process_init` 把主堆、元数据堆、page map 全部建好。但分配是**每线程**的事——`mi_malloc` 的第一步永远是「拿到当前线程的默认 theap」。本讲专门回答三个问题：

1. 一个线程如何、以多大代价拿到自己的 `mi_theap_t`？为什么说这次访存是分配快路径的成本下限？
2. mimalloc 为什么在 `prim-tls.h` 里维护 LOCAL / PTHREADS / WIN32 / FIXED 四种 TLS 策略，它们各自的取数路径长什么样？
3. 线程退出时，TLS destructor 如何联动 `_mi_thread_done`，把线程的页面 abandon 掉而不是泄漏？

学完后你应该能：画出 Linux 与 Windows 两条「取默认 theap」的调用图，数出各自需要几次内存访问，并解释 `_mi_theap_default` 与 `_mi_theap_cached` 这两个 TLS 槽位（两级缓存）以及支撑它们的 theap 引用计数。

## 2. 前置知识

**线程本地存储（TLS，Thread-Local Storage）**：同一份变量名，每个线程各持一份私有副本，互不可见。它是实现「每线程一个分配堆」这类设计的基础设施。实现 TLS 大体有三条路线：

| 路线 | 形态 | 访问方式 | 典型代价 |
|---|---|---|---|
| 编译器 TLS | C11 `_Thread_local` / GCC `__thread` | 编译成段寄存器（x86-64 Linux 为 `fs`）加固定偏移的一次 load | 约 1 次访存 |
| pthread key | `pthread_key_create` + `pthread_getspecific` | 查线程描述符里的 specific 数组 | 约 2～3 次访存 |
| Windows TLS slot | `TlsAlloc` + `TlsGetValue` | 查 TEB（线程环境块）里的槽位数组 | 约 1～2 次访存 |

**ELF TLS 寻址模型**：Linux 下编译器对 `_Thread_local` 变量有 local-exec、initial-exec、global-dynamic 等寻址模型。initial-exec 意为「假设模块加载时 TLS 块偏移已定」，于是访问被编译为 `mov rax, fs:偏移` 这一条指令——这正是 mimalloc 在 Linux 上选择的模型（`-ftls-model=initial-exec` 一类的效果），也是「取 theap 只要 1 次访存」的来源。代价是它不适用于 `dlopen` 动态加载场景的某些限制，但对分配器来说值得。

**TEB 与直接槽**：Windows 每个线程有一个 TEB（Thread Environment Block），其中前 64 个 TLS 槽是「直接槽」，`__readgsqword(offset)` 一条指令即读；超过 64 个 `TlsAlloc` 会落入「扩展槽」数组，需要多一次间接寻址。这个 64 的分界线稍后会直接出现在源码里。

**引用计数（refcount）**：结构体里放一个原子计数，有人持有就加一、放手就减一，减到零才真正释放。本讲会看到：仅仅因为「一个 theap 指针被存进了 TLS 缓存槽」，就足以让 theap 需要引用计数。

**承接前置讲的认知**：u3-l1 已建立五层模型 subproc → heap → theap → page queue → page → block；u4-l1 已确认快路径是「TLS 取默认 theap → `pages_free_direct` 直查定页 → 弹块」。本讲就是把那条链的第一步拆开到底。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/mimalloc/prim-tls.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h) | TLS 接口头：声明 `_mi_theap_default()` 等内联函数，并用宏分支实现 LOCAL / PTHREADS / WIN32 / FIXED 四种模型。快路径内联就发生在这里。 |
| [src/prim/prim-tls.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c) | TLS 模型的**非内联**部分：thread local 变量定义、Windows `TlsAlloc` 槽位协商、pthread key 创建，以及 `_mi_theap_default_set` / `_mi_theap_cached_set` 两个写入口。 |
| [src/threadlocal.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c) | mimalloc **自制**的动态 TLS：不受 OS 限制的「无限线程局部变量」，每个一等堆一个槽，用版本号 + 位图管理槽位复用。 |
| [src/theap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c) | theap 的生命周期：`mi_theap_get_default` 惰性初始化入口、`_mi_theap_incref/_mi_theap_decref` 引用计数。 |
| [src/init.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c) | `_mi_thread_init_with_heap`（线程初始化、写 TLS）与 `_mi_thread_done`（线程收尾）。 |
| [src/prim/unix/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c) | Unix 平台线程终止检测：pthread key 的 destructor 调 `_mi_thread_done`。 |

## 4. 核心概念与源码讲解

### 4.1 快路径上的第一次访存：四种 TLS 模型总览

#### 4.1.1 概念说明

回看 u4-l1 的结论：release 下 `mi_malloc` 小对象快路径总共约 8 次访存，其中第一次就是从 TLS 读默认 theap 指针。源码里这体现为 [src/alloc.c:209-214](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L209-L214) 这对孪生入口——不带 heap 参数的 API 第一步取 `_mi_theap_default()`，带 heap 参数的 API 取 `_mi_heap_theap(heap)`：

```c
void* mi_malloc_small(size_t size) mi_attr_noexcept {
  return mi_theap_malloc_small(_mi_theap_default(), size);
}
void* mi_heap_malloc_small(mi_heap_t* heap, size_t size) mi_attr_noexcept {
  return mi_theap_malloc_small_zero_nonnull(_mi_heap_theap(heap), size, false, NULL);
}
```

`_mi_theap_default()` 是内联进 `mi_malloc` 的，它的实现方式直接决定每次分配的固定开销。问题在于：**「读一个线程局部指针」在不同 OS 上没有统一的便宜做法**。Linux 的 `_Thread_local` 极快；但 macOS 的加载器会在首次访问 thread local 时触发 `malloc`（分配器递归调用自己！）；Windows 用 TEB 槽最自然。于是 mimalloc 在 `prim-tls.h` 里维护四套实现，按平台自动选择一套：

- **MI_TLS_MODEL_LOCAL**（Linux、FreeBSD 等默认）：直接用编译器 thread local，初始值指向静态空 theap。
- **MI_TLS_MODEL_PTHREADS**（macOS、OpenBSD、Android 默认）：用 `pthread_getspecific`，macOS 上它反而比 thread local 更稳（不会在首次访问时分配）。
- **MI_TLS_MODEL_WIN32**（Windows 默认）：`TlsAlloc` 申请 TEB 槽，优先落进前 64 个直接槽。
- **MI_TLS_MODEL_FIXED**（可选）：写死一个 TEB 槽号，最快但与其他库冲突风险自负。

头文件顶部的注释就是这张地图的官方版：[include/mimalloc/prim-tls.h:22-26](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L22-L26) 列出平台到模型的默认对应关系。

#### 4.1.2 核心流程

默认模型的自动选择逻辑只有 9 行：

```text
若用户未显式定义任何 MI_TLS_MODEL_*：
  _WIN32                        → MI_TLS_MODEL_WIN32
  __APPLE__/__OpenBSD__/__ANDROID__ → MI_TLS_MODEL_PTHREADS
  其余（Linux、FreeBSD 等）      → MI_TLS_MODEL_LOCAL
```

而每个模型必须回答同一个问题：「`_mi_theap_default()` 的**初始值**（线程尚未初始化时）是 NULL 还是 `&_mi_theap_empty`？」这由 `MI_THEAP_INITASNULL` 宏标记：LOCAL 模型不定义它（初始值是静态空 theap，永不为 NULL，快路径省一次判空）；其余三个模型都定义它（初始为 NULL，需要判空）。

#### 4.1.3 源码精读

默认模型选择在 [include/mimalloc/prim-tls.h:42-50](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L42-L50)，这段代码决定你手头的构建走哪条路：

```c
#if !defined(MI_TLS_MODEL_LOCAL) && !defined(MI_TLS_MODEL_PTHREADS) && !defined(MI_TLS_MODEL_FIXED) && !defined(MI_TLS_MODEL_WIN32)
#if defined(_WIN32)
#define MI_TLS_MODEL_WIN32        1
#elif defined(__APPLE__) || defined(__OpenBSD__) || defined(__ANDROID__)
#define MI_TLS_MODEL_PTHREADS     1
#else
#define MI_TLS_MODEL_LOCAL        1
#endif
#endif
```

LOCAL 模型的取数实现是四套里最短的，[include/mimalloc/prim-tls.h:247-264](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L247-L264)：两个 thread local 指针 + 两个只读直返的内联函数。

```c
extern mi_decl_hidden mi_decl_thread mi_theap_t* __mi_theap_default;  // default theap to allocate from
extern mi_decl_hidden mi_decl_thread mi_theap_t* __mi_theap_cached;   // theap from the last used heap

static inline mi_theap_t* _mi_theap_default(void) {
  #if defined(MI_TLS_RECURSE_GUARD)
  if mi_unlikely(!_mi_process_is_initialized) return _mi_theap_empty_get();
  #endif
  return __mi_theap_default;
}
```

两个要点：

1. `__mi_theap_default` 与 `__mi_theap_cached` 就是本讲的两个主角——「两级缓存」的两个槽位：前者服务不带 heap 参数的 `mi_*` API，后者缓存「最近一次经 `_mi_heap_*` API 用过的 theap」。它们的定义在 [src/prim/prim-tls.c:25-30](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L25-L30)，初始值都指向静态空 theap `_mi_theap_empty`：
   ```c
   mi_decl_hidden mi_decl_thread mi_theap_t* __mi_theap_default = (mi_theap_t*)&_mi_theap_empty;
   mi_decl_hidden mi_decl_thread mi_theap_t* __mi_theap_cached  = (mi_theap_t*)&_mi_theap_empty;
   ```
   `_mi_theap_empty` 是 init.c 里的只读模板（定义于 [src/init.c:120](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L120)，声明于 [include/mimalloc/internal.h:626-627](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L626-L627)）。空 theap 的 `heap` 字段为 NULL，因此它同时是「未初始化」的标记——[include/mimalloc/internal.h:640-642](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L640-L642) 把「是否已初始化」定义为 `heap` 字段是否非 NULL：
   ```c
   static inline bool mi_theap_is_initialized(const mi_theap_t* theap) {
     return (theap != NULL && _mi_theap_heap_peek(theap) != NULL);
   }
   ```
   这正是 u3-l1 讲过的设计：新线程首次 `mi_malloc` 时拿到空 theap，`pages_free_direct` 自然落空，顺滑滑进慢路径，无需任何特判。

2. `MI_TLS_RECURSE_GUARD` 是给 macOS 强制 LOCAL 模型时兜底的：因为 macOS 的 thread local 首次访问可能触发分配（递归！），加一道「进程是否已初始化」的检查，见 [include/mimalloc/prim-tls.h:231-233](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L231-L233)。注释明确说这会往快路径塞一个检查，能避则避——这也是 macOS 默认走 PTHREADS 的原因。

四套模型的完整设计说明写在 [include/mimalloc/prim-tls.h:193-229](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L193-L229) 的大注释块里，值得整段通读——它逐条解释每种模型的动机、风险与 `MI_THEAP_INITASNULL` 的含义，是本讲最好的第一手教材。

#### 4.1.4 代码实践

1. **实践目标**：确认你手头平台默认走哪条 TLS 模型分支。
2. **操作步骤**：
   - 在 Linux 上执行 `echo | gcc -dM -E -x c - | grep -E '__linux__|__GLIBC__|__APPLE__|_WIN32'`，查看预定义宏。
   - 对照 [prim-tls.h:42-50](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L42-L50) 推导：`__linux__` 已定义、`_WIN32`/`__APPLE__` 未定义 → 命中 `#else` → `MI_TLS_MODEL_LOCAL`。
   - 再执行 `grep -rn "MI_TLS_MODEL" CMakeLists.txt` 看有哪些 cmake 开关可以强制改模型（如 `MI_TLS_FIXED`、`MI_WIN_DIRECT_TLS` 相关选项）。
3. **需要观察的现象**：Linux 上 `__linux__` 出现在宏列表中；CMakeLists 中存在能覆盖默认模型的选项。
4. **预期结果**：得出结论「Linux 默认构建 = MI_TLS_MODEL_LOCAL，`_mi_theap_default()` 展开后是一次 fs 段寻址的 load」。
5. 待本地验证（宏列表输出依编译器版本而异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MI_THEAP_INITASNULL` 会「在快路径多一个检查」？这个检查能省掉的前提是什么？

**答案**：若 TLS 初始值可能是 NULL（如 pthread key 创建前的窗口、Windows 扩展槽未初始化的线程），`_mi_theap_default()` 的调用方就必须判空后才能解引用。LOCAL 模型把初始值定为 `&_mi_theap_empty`——一个永远合法的只读结构体，指针永不为 NULL，于是判空可以并入既有的「是否初始化」判断甚至完全省去；代价是链接期就要有一个完整的静态空 theap 模板。

**练习 2**：`_mi_theap_default()` 在 LOCAL 模型下读一个 thread local 指针，为什么说它是「分配快路径的成本下限」？

**答案**：任何不带 heap 参数的 `mi_malloc` 在做任何有用功之前都必须先知道「在哪个 theap 上分配」，这一步无法跳过、无法缓存到寄存器（线程可能被调度走再回来，但 TLS 值只被本线程改写，编译器其实可跨调用缓存——不过 mimalloc 的内联层级不保证）；它展开为 1 次访存，是每次分配至少要付出的固定成本，之后的 `pages_free_direct` 查页（1 次）与弹块（1～2 次）都建立在它之上。

### 4.2 平台取数实现精读：Linux、macOS、Windows 三条路径

#### 4.2.1 概念说明

本模块逐个打开三条真实取数路径。它们要解决的核心矛盾是：**「最快的 TLS」因平台而异，而且最快的方案常有副作用**（递归分配、槽位冲突、初始化窗口期返回 NULL）。mimalloc 的做法是每平台挑一条默认路径，并把副作用用小技巧消化掉。

#### 4.2.2 核心流程

三条路径的取数流程（N 为访存次数的量级估计）：

```text
Linux (LOCAL, initial-exec):
  fs:offset ──load──> __mi_theap_default        N = 1

macOS (PTHREADS):
  load _mi_theap_default_key (普通原子 relaxed load)
  ──> key==INVALID ? NULL
  ──> mi_prim_tls_slot(key)   [Apple arm64 直读 pthread TSD 槽]
                              N ≈ 2

Windows (WIN32):
  load _mi_theap_default_slot ──> slot
  ──> mi_prim_tls_slot(slot): __readgsqword(slot*8)   N = 2（直接槽）
  ── 若 slot==扩展槽指针位置: 再 load expansion_slot 下标、
     load eslots[i]           N = 4（扩展槽）
```

写成公式：设 \( N \) 为 `_mi_theap_default()` 的访存次数，\( E \in \{0,1\} \) 指示是否落入扩展槽，则 Windows 模型下 \( N_{\text{WIN32}} = 2 + 2E \)，而 \( N_{\text{LOCAL}} = 1 \)、\( N_{\text{FIXED}} = 1 \)。u4-l1 数出的「8 次访存」里，Linux 上第 1 次就花在这里。

#### 4.2.3 源码精读

**PTHREADS 路径**（macOS/OpenBSD/Android），[include/mimalloc/prim-tls.h:275-285](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L275-L285)：

```c
static inline mi_theap_t* _mi_theap_default(void) {
  pthread_key_t key = mi_atomic_load_relaxed(&_mi_theap_default_key);
  #if defined(__APPLE__) && defined(__aarch64__) && MI_HAS_TLS_SLOT
  if (key == MI_PTHREAD_KEY_INVALID) return NULL;
  return (mi_theap_t*)mi_prim_tls_slot(key);
  #else
  return (mi_theap_t*)mi_pthread_key_get(key);
  #endif
}
```

- pthread key 存在原子变量 `_mi_theap_default_key` 里（初始 `MI_PTHREAD_KEY_INVALID`，见 [src/prim/prim-tls.c:154-155](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L154-L155)），用 relaxed load 读取——key 一旦发布不再变化，无需同步语义。
- Apple arm64 上 pthread key 恰好就是 TSD 数组下标，于是绕开 `pthread_getspecific` 的函数调用开销，用 `mi_prim_tls_slot`（thread pointer 加下标）直读，注释说这避免了 `mi_malloc` 里多余的栈帧建立。
- 其余平台走 `mi_pthread_key_get`，它只是 `pthread_getspecific` 的小封装（glibc 上对非法 key 恒返回 NULL，可省一次判断），见 [include/mimalloc/internal.h:431-436](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L431-L436)。

**WIN32 路径**，[include/mimalloc/prim-tls.h:314-326](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L314-L326)：

```c
static inline mi_theap_t* _mi_theap_default(void) {
  const size_t slot = mi_atomic_load_relaxed(&_mi_theap_default_slot);
  mi_theap_t* theap  = (mi_theap_t*)mi_prim_tls_slot(slot);
  #if !MI_WIN_DIRECT_TLS
  if mi_unlikely(slot==MI_TLS_EXPANSION_SLOT) {       // in TlsExpansionSlots ?
    mi_theap_t** const eslots = (mi_theap_t**)theap;  // theap is actually the expansion slot entry
    if mi_likely(eslots!=NULL) {                      // is it initialized? (on this thread)
      theap = eslots[mi_atomic_load_relaxed(&_mi_theap_default_expansion_slot)];
    }
  }
  #endif
  return theap;
}
```

读懂它需要知道两个事实：

1. `mi_prim_tls_slot(slot)` 是「TEB 基址 + slot×8」的一次 load，x64 MSVC 下直接编译成 `__readgsqword`，见 [include/mimalloc/prim-tls.h:141-149](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L141-L149)；TEB 基址来自 `NtCurrentTeb()`（[prim-tls.h:79-82](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L79-L82)）。
2. `MI_TLS_EXPANSION_SLOT` 是 TEB 中「TLS 扩展槽数组指针」字段的位置（64 位下 `0x1780/8`，见 [prim-tls.h:303-307](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L303-L307)）。mimalloc 的巧招是：**初始化之前就把 slot 设成这个位置**（[src/prim/prim-tls.c:61-62](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L61-L62) 与 [prim-tls.c:74-77](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L74-L77)），因为扩展槽数组指针在线程未用过扩展槽时是 NULL——读出来自然是 NULL，完美充当「初始空值」，不需要额外保留一个直接槽。

真正申请槽位的代码是 `mi_win_tls_slot_alloc`，[src/prim/prim-tls.c:82-112](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L82-L112)：`TlsAlloc()` 返回的索引若小于 64，折算成直接槽号 `index + MI_TLS_DIRECT_FIRST`（0x1480/8，[prim-tls.c:48-54](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L48-L54)）；否则落到扩展槽数组，slot 固定为 `MI_TLS_EXPANSION_SLOT`、把真实下标存进 `_mi_theap_default_expansion_slot`。注意第 83 行注释「always write slot before extended due to concurrent readers」——两个原子变量必须按此顺序发布，否则并发读线程可能读到新 slot 配旧下标。

**FIXED 模型**（可选）干脆不申请，直接用写死的槽号，[include/mimalloc/prim-tls.h:369-375](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L369-L375)：`return (mi_theap_t*)mi_prim_tls_slot(MI_TLS_MODEL_FIXED_DEFAULT);`。macOS 上挑的是 swift 框架预留槽 108/109、Windows 上挑 TEB 的 5/7 号字段（[prim-tls.h:352-367](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L352-L367)）。头文件注释直言其风险：OS 或其他库用了同一槽就出错，且进程内不能同时存在两份 mimalloc。初始化时的自检在 [src/prim/prim-tls.c:181-192](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L181-L192)：发现槽里已有非 NULL 值就报 `EINVAL` 错误。

**线程 id 的顺带收获**：同一个 `mi_prim_thread_pointer()` 也被用来生成线程 id（free 路径判定页属主用的就是它），见 [include/mimalloc/prim-tls.h:169-176](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L169-L176)——thread pointer 本身就是每线程唯一的地址。这也解释了 u3-l2 讲过的「线程 id 低 2 位恒为 0」：它是按指针对齐的。没有 thread pointer 的平台则退化为取一个 thread local 变量的地址（[prim-tls.h:178-183](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L178-L183)，变量定义在 [src/prim/prim-tls.c:32](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L32)）。

#### 4.2.4 代码实践

1. **实践目标**：画出 Linux(pthread/LOCAL) 与 Windows(TLS slot) 两条「获取默认 theap」的调用图，并数出各自访存次数。
2. **操作步骤**：
   - 对 Linux：从 `mi_malloc`（[src/alloc.c:209-211](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L209-L211)）出发 → `_mi_theap_default()`（[prim-tls.h:255-260](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L255-L260)）→ `__mi_theap_default`（[prim-tls.c:27](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L27)），标注「fs 段寻址，1 次 load」。
   - 对 Windows：`mi_malloc` → `_mi_theap_default()`（[prim-tls.h:314-326](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L314-L326)）→ `mi_prim_tls_slot`（[prim-tls.h:141-149](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L141-L149)）→ `NtCurrentTeb`（[prim-tls.h:79-82](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L79-L82)），并画出扩展槽分支的两次额外 load。
   - 想看真实汇编的话：在 Linux 上 `cc -O2 -S` 一个仅调用 `mi_malloc` 的小程序并链接 mimalloc 静态库，在生成的汇编中找 `mov ... fs:` 形式的指令（u1-l2 讲过 debug 构建与 `-DMI_SEE_ASM` 相关开关；本步为可选）。
3. **需要观察的现象**：Linux 路径图只有 3 个节点、1 次 load；Windows 路径图有分支（直接槽 / 扩展槽），分别 2 次与 4 次 load。
4. **预期结果**：得出对比表——\( N_{\text{LOCAL}}=1 \)、\( N_{\text{WIN32}}=2+2E \)、\( N_{\text{PTHREADS}}\approx 2 \)。
5. 汇编观察部分待本地验证（依赖编译器与链接方式）。

#### 4.2.5 小练习与答案

**练习 1**：Windows 初始化前把 slot 设为 `MI_TLS_EXPANSION_SLOT` 而不是某个直接槽，这个「借扩展槽指针当 NULL」的技巧有什么隐患？源码注释提到了什么？

**答案**：隐患是若程序在 mimalloc 初始化前就用满了 64 个直接槽加 1024 个扩展槽（恰好把最后一个扩展槽也分出去），初始读数就不再是 NULL。[src/prim/prim-tls.c:57-59](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L57-L59) 的注释原话是「this will fail if the program allocates exactly 1024+64 slots with TlsAlloc before we are initialized :-( (but this seems quite unlikely)」——作者权衡后认为概率极低，可接受。

**练习 2**：`_mi_theap_default_slot` 为什么用 `mi_atomic_load_relaxed` 而不是普通读？又为什么不需要 acquire？

**答案**：用原子读是因为该变量会被另一个线程在初始化时以 release 语义发布（见 `mi_win_tls_slot_alloc` 中的 `mi_atomic_store_release`），普通读在 C 内存模型下属于数据竞争。不需要 acquire 是因为 slot 只是下标，不承载「其所指内存已初始化」的发布语义——TlsSetValue 本身有系统级同步，且读错也只是拿到旧的一致值（初始槽），无害。

### 4.3 mimalloc 自制的动态 TLS：threadlocal.c 与 heap→theap 槽

#### 4.3.1 概念说明

四模型解决的是「**每线程一个**默认 theap 指针」的存取。但 u3-l1 讲过：一个 heap 在**每个**使用它的线程里都有一个专属 theap，即「heap × 线程」的二维表——每行（每堆）都需要一个独立的线程局部槽。OS 的 pthread key 有数量上限（Linux 通常 1024 个）、Windows 扩展槽也有限，用户程序若创建数千个一等堆就会耗尽。于是 mimalloc 在 [src/threadlocal.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c) 里实现了**不受数量限制的动态线程局部变量**：每个线程持有一个可增长的槽位数组，key 里编码「槽下标 + 版本号」，全局用一张原子位图分配与回收槽位。

#### 4.3.2 核心流程

```text
写 heap 的 theap（_mi_heap_theap_set）:
  key = heap->theap                     # 建堆时分配好的 key
  若 key == mi_thread_local_key_fast (1): 走 mi_slot_fast（专用快速槽）
  否则: idx = key 低 16 位
        若 idx < 槽数组长度: slots[idx] = {value, version}   # 快路径，2 次写
        否则: 扩容数组（16 起步，翻倍，1024 后线性增长）再写

读（_mi_thread_local_get）:
  key==fast → mi_slot_fast_get()
  否则: 取本线程槽位数组 → idx 与 version 都匹配才返回 value，否则 NULL

key 的分配（_mi_thread_local_create，进程级，持锁）:
  在全局位图里找一个空闲位并占用 → 全局 version++ → 返回 (version<<16)|index
```

版本号的作用：槽位被回收再分配后，旧的 key（拿着旧 version）来读会因版本不匹配得到 NULL，**防止读到别人后来存进同一槽的值**。

#### 4.3.3 源码精读

整套机制的根是**两个**真正的 OS 级线程局部变量——其余一切槽数组都挂在它们上面。宏 `mi_define_thread_local` 按平台二选一实现，[src/threadlocal.c:44-61](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L44-L61)：pthread 平台（含 macOS）用 pthread key，否则直接 `mi_decl_thread` 变量。随后实例化出这两个根变量，[src/threadlocal.c:63-64](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L63-L64)：

```c
mi_define_thread_local(mi_thread_locals_t*, mi_thread_locals, &mi_thread_locals_empty)
mi_define_thread_local(void*,            mi_slot_fast,    NULL)
```

- `mi_thread_locals`：指向本线程的槽位数组（`mi_thread_locals_t`，含 count、memid 与变长 slots，定义见 [threadlocal.c:23-32](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L23-L32)）。
- `mi_slot_fast`：**专用单槽**，给主堆专用——因为主堆的 theap 就是默认 theap，访问最频繁，值得单独一个不查数组、不比版本的槽。

key 的编码在 [src/threadlocal.c:74-83](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L74-L83)：64 位平台上低 16 位放槽下标、高 48 位放版本号。而常量 `mi_thread_local_key_fast = 1` 定义在 [include/mimalloc/internal.h:240](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L240)，主堆初始化时直接把它当 key 用（[src/init.c:198](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L198)），普通堆则在建堆时现申请一个真 key（[src/heap.c:136-146](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L136-L146)）：

```c
mi_thread_local_t theap_slot = (is_main_heap ? mi_thread_local_key_fast : _mi_thread_local_create());
```

读写入口把 fast 槽与普通槽分流，[src/threadlocal.c:165-173](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L165-L173)：

```c
bool _mi_thread_local_set( mi_thread_local_t key, void* val ) {
  if (key == mi_thread_local_key_fast) {
    return mi_slot_fast_set(val);
  } else {
    return mi_thread_local_set_regular(key,val);
  }
}
```

普通读路径带边界与版本双重校验，[src/threadlocal.c:176-192](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L176-L192)：

```c
static mi_decl_noinline void* mi_thread_local_get_regular( mi_thread_local_t key ) {
  const mi_thread_locals_t* const tls = mi_thread_locals_peek();
  if mi_unlikely(tls==NULL) { return NULL; }   // 线程已收尾，数组已释放
  const size_t idx = mi_key_index(key);
  if mi_likely(idx < tls->count && mi_key_version(key) == tls->slots[idx].version) {
    return tls->slots[idx].value;
  } else {
    return NULL;
  }
}
```

注意第 179-184 行的注释：线程退出后数组已被释放，这里必须容忍 NULL——统计打印时仍可能来查。

数组装不下时按 16 起步、先翻倍、超 1024 后线性加 1024 地扩容，并把旧内容搬进新数组（`mi_thread_locals_expand`，[src/threadlocal.c:104-131](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L104-L131)），内存走元数据分配（`_mi_meta_rezalloc`），因此 secure 模式下也安全。

key 的全局分配复用了 arena 的原子位图设施：`mi_thread_local_claim` 在位图里找一个空闲位并占用、全局版本号加一（[src/threadlocal.c:251-261](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L251-L261)），位图不够就再扩 1024 位（[threadlocal.c:263-286](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L263-L286)），对外入口是持锁的 `_mi_thread_local_create`（[threadlocal.c:290-303](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L290-L303)）。堆销毁时把位归还（`_mi_thread_local_free`，[threadlocal.c:306-315](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L306-L315)；heap.c:224 的调用点）。

最后把两个体系接起来——写一个 heap 的 theap 只有一行，[src/heap.c:37-42](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L37-L42)：

```c
bool _mi_heap_theap_set(mi_heap_t* heap, mi_theap_t* theap) {
  ...
  return _mi_thread_local_set(heap->theap, theap);
}
```

#### 4.3.4 代码实践

1. **实践目标**：跟踪「`mi_heap_new` → 槽位分配 → 线程首次在该堆分配 → 槽位命中」这条链，理解 heap→theap 映射的存取。
2. **操作步骤**：
   - 依次阅读 [heap.c:136](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L136)（建堆分 key）、[heap.c:37-42](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L37-L42)（写槽）、[heap.c:90-100](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L90-L100)（`_mi_heap_theap_get_or_init`：读槽为空则创建并回填）、[threadlocal.c:195-203](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L195-L203)（读槽分流）。
   - 画出时序：`_mi_heap_theap_get_or_init` 第一次在某线程调用时，`_mi_thread_local_get(heap->theap)` 返回 NULL → `mi_heap_init_theap` 创建 theap → `_mi_heap_theap_set` 写入槽位；第二次调用直接命中。
3. **需要观察的现象**：读路径需要「数组指针 load + 边界判断 + 版本比对 + 值 load」约 3 次访存，明显贵于 `_mi_theap_cached` 的 1 次——这正是 cached 槽存在的理由。
4. **预期结果**：能口头复述「为什么带 heap 参数的 API 需要一层 cached theap 缓存，而不是每次都查 heap 的线程局部槽」。
5. 无需运行，属源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：为什么主堆的 theap 槽要用专用的 `mi_slot_fast`，而不是像其他堆一样申请普通 key？

**答案**：主堆是默认堆，它的 theap 即默认 theap，是全库访问频率最高的线程局部值（每次不带 heap 参数的分配都要用）。专用单槽省去「数组指针 load + 下标边界检查 + 版本比对」三次访存中的前两次，直接一次 load 得值。这是用两个 OS 级 TLS 变量中宝贵的一个换性能。

**练习 2**：key 里 48 位版本号解决什么问题？如果去掉版本号只用下标，会出什么错？

**答案**：堆 A 销毁后其槽位被回收，随后堆 B 创建时拿到同一个下标。若某线程因时序原因仍持旧 key 来读，无版本号就会读到堆 B 的 theap，把对象分配进错误的堆；有版本号则比对失败返回 NULL，安全地走「重新初始化」路径。版本号全局递增（回绕时归 1），保证新 key 几乎不可能与任意旧 key 相同。

### 4.4 写入路径、引用计数与线程退出的 destructor 联动

#### 4.4.1 概念说明

读路径看完了，本模块看**写**：谁在什么时候把真正的 theap 写进这两个槽，写的时候要额外付什么代价，以及线程退出时这套机制如何被反向拆除。两条主线：

1. **`_mi_theap_default_set` / `_mi_theap_cached_set`** 是所有模型统一的写入口。cached 槽的写入必须伴随 theap 引用计数的一增一减——因为被缓存的 theap 可能随时被别的路径销毁，得保证「还在缓存里就还活着」。
2. **线程退出的钩子**：Unix 用一个 pthread key 的 destructor、Windows 用 `DLL_THREAD_DETACH`/FLS 回调，最终都汇入 `_mi_thread_done`，把该线程所有 theap 的页面 abandon（承接 u6-l4）。微妙之处在于：**线程终止时刻 TLS 可能已经不可用**（访问它会再次触发分配），所以默认 theap 必须提前「报备」给平台层。

#### 4.4.2 核心流程

```text
线程初始化（首次分配触发或显式 mi_thread_init）:
  _mi_thread_init_with_heap:
    创建 tld → 分配/复用 theap → _mi_theap_init
    → _mi_theap_default_set(theap)        # 写槽①；内部还会把 theap 报备给
                                          # _mi_prim_thread_associate_default_theap
    → _mi_heap_theap_set(heap_main, theap)# 写槽②（threadlocal.c 的 fast 槽）

带 heap 的分配:
  _mi_heap_theap(heap):
    t = _mi_theap_cached()                # 先查缓存槽
    若 t->heap == heap → 直接用           # 1 次访存命中
    否则 _mi_heap_theap_get_or_init(heap) # 查 heap 的线程局部槽，必要时创建，
                                          # 并 _mi_theap_cached_set 刷新缓存（含引用计数）

线程退出（Unix）:
  pthread destructor mi_pthread_done(value)
    → _mi_thread_done(theap)
      → _mi_thread_locals_thread_done()   # 释放本线程槽数组
      → mi_thread_theaps_done(tld):
          逐 theap abandon 页面
          _mi_theap_default_set(_mi_theap_empty)   # 两个槽复位为空 theap
          _mi_theap_cached_set(_mi_theap_empty)
          逐 theap _mi_theap_decref       # 引用归零者释放内存
```

#### 4.4.3 源码精读

**统一写入口**。`_mi_theap_default_set` 按四个模型各写一行，末尾还有一步容易被忽略的「报备」，[src/prim/prim-tls.c:231-252](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L231-L252)：

```c
void _mi_theap_default_set(mi_theap_t* theap)  {
  ...
  #if MI_TLS_MODEL_LOCAL
    __mi_theap_default = theap;
  #elif MI_TLS_MODEL_FIXED
    mi_prim_tls_slot_set(MI_TLS_MODEL_FIXED_DEFAULT, theap);
  #elif MI_TLS_MODEL_WIN32
    mi_win_tls_slot_set(...);
  #elif MI_TLS_MODEL_PTHREADS
    pthread_key_t key = mi_atomic_load_relaxed(&_mi_theap_default_key);
    if (key!=MI_PTHREAD_KEY_INVALID) { pthread_setspecific(key, theap); }
  #endif

  // set theap main if needed
  if (mi_theap_is_initialized(theap)) {
    // ensure the default theap is passed to `_mi_thread_done` as on some platforms
    // we cannot access TLS at thread termination (as it would allocate again)
    _mi_prim_thread_associate_default_theap(theap);
  }
}
```

最后那行调用的 Unix 实现是 `pthread_setspecific(_mi_heap_default_key, theap)`，[src/prim/unix/prim.c:1036-1040](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L1036-L1040)。`_mi_heap_default_key` 是一个**带 destructor 的 pthread key**，创建于 [src/prim/unix/prim.c:1023-1026](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L1023-L1026)，destructor 是 [src/prim/unix/prim.c:1017-1021](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L1017-L1021)：

```c
static void mi_pthread_done(void* value) {
  if (value!=NULL) {
    _mi_thread_done((mi_theap_t*)value);
  }
}
```

pthread 规范保证：线程退出时，其所有非空 specific 值的 destructor 会被调用。于是「默认 theap 被设置过」这件事本身就注册了线程收尾的钩子——**这就是 TLS destructor 联动 `_mi_thread_done` 的那条线**。这也解释了为什么报备时传的是 theap 指针：`_mi_thread_done` 在 TLS 已不可靠的时点仍能从参数拿到 it（[src/init.c:452-457](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L452-L457) 中 `NULL` 参数才回退去读 `_mi_theap_default()`）。Windows 的对应物是 `DLL_THREAD_DETACH` 与 FLS 回调（[src/prim/windows/prim.c:775](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L775)、[src/prim/windows/prim.c:1139-1140](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L1139-L1140)）。

**cached 槽写入与引用计数**。`_mi_theap_cached_set` 末尾的三行是本模块的账目核心，[src/prim/prim-tls.c:211-229](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L211-L229)：

```c
void _mi_theap_cached_set(mi_theap_t* theap) {
  mi_theap_t* prev = _mi_theap_cached();
  if (prev==theap) return;
  ...（按模型写槽）...
  // update refcounts (so cached theap memory keeps available until no longer cached)
  _mi_theap_incref(theap);
  _mi_theap_decref(prev);
}
```

theap.c 里的注释直说了动机——[src/theap.c:357-370](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L357-L370)：

```c
// we need to reference count theaps due to the _mi_theap_cached thread locals
void _mi_theap_incref(mi_theap_t* theap) { ... mi_atomic_increment_acq_rel(&theap->refcount); }
void _mi_theap_decref(mi_theap_t* theap) {
  ...
  if (mi_atomic_decrement_acq_rel(&theap->refcount) == 1) { mi_theap_free_mem(theap); }
}
```

（ decref 判断 `== 1` 是「减完之后为 0」的原子写法。）静态分配的 theap（`memid` 标记为 static/无需释放，如 `_mi_theap_empty`）被 `mi_memid_needs_no_free` 排除，永不计数。

**两级缓存的读侧**。`_mi_heap_theap(heap)` 先试 cached 槽、不行才查（并可能创建）heap 的线程局部槽，[include/mimalloc/prim-tls.h:389-397](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L389-L397)：

```c
static inline mi_theap_t* _mi_heap_theap(mi_heap_t* heap) {
  mi_theap_t* theap = _mi_theap_cached();
  #if MI_THEAP_INITASNULL
  if mi_likely(theap!=NULL && _mi_theap_heap_peek(theap)==heap) return theap;
  #else
  if mi_likely(_mi_theap_heap_peek(theap)==heap) return theap;
  #endif
  return _mi_heap_theap_get_or_init(heap);
}
```

判据极妙：不需要在缓存里存「是哪个堆的 theap」的旁表，直接比对 `theap->heap == heap`（一次 relaxed 原子读，[include/mimalloc/internal.h:630-632](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L630-L632)）。典型程序里一个线程长期只用一个堆，cached 命中率极高，命中时仅 2 次访存（cached 槽 + heap 字段）。

**线程初始化的写序**。[src/init.c:349-352](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L349-L352) 固定了顺序：

```c
  // now initialize the thread
  _mi_theap_default_set(theap);
  // and only then set the heap_theap field as that accesses thread locals
  _mi_heap_theap_set(heap_main, theap);  // todo: can fail!
```

必须先 default 后 heap 槽：`_mi_heap_theap_set` 走 threadlocal.c 的读改路径，中途可能触发分配，而那时 default 槽必须已指向可用 theap，否则递归初始化。

**收尾**。`_mi_thread_done`（[src/init.c:452-481](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L452-L481)）先释放动态槽数组（`_mi_thread_locals_thread_done`，[src/threadlocal.c:205-214](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/threadlocal.c#L205-L214)：把数组交还元数据堆并把两个根 TLS 变量清空），再 abandon 页面、把两个槽复位为空 theap（[src/init.c:396-397](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L396-L397)），最后逐 theap 减引用（[src/init.c:404-418](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L404-L418)，断言 refcount==1——因为缓存槽刚被复位）。另有一个跨线程边界：pthread 模型下 **cached key 自己的 destructor** 只做一件事——把缓存里那个 theap 减引用（[src/prim/prim-tls.c:157-162](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L157-L162)）：

```c
static void mi_theap_cached_key_destroy(void* theapv) {
  mi_theap_t* theap = (mi_theap_t*)theapv;
  if (theap!=NULL) {
    _mi_theap_decref(theap);
  }
}
```

即：线程退出时，它通过 cached 槽持有的那份引用由 pthread 机制自动还清。

#### 4.4.4 代码实践

1. **实践目标**：观察「线程创建 → TLS 写入 → 线程退出 → destructor 联动 abandon」的全过程。
2. **操作步骤**（以下程序为**示例代码**，非仓库原有文件）：
   ```c
   // tls_obs.c: 观察每线程 theap 的生灭
   #include <mimalloc.h>
   #include <pthread.h>
   #include <stdio.h>
   #include <unistd.h>

   static void* worker(void* arg) {
     void* p = mi_malloc(100);          // 首次分配触发 _mi_thread_init_with_heap
     mi_free(p);
     return NULL;                        // 线程退出 → mi_pthread_done → _mi_thread_done
   }

   int main(void) {
     for (int i = 0; i < 4; i++) {
       pthread_t t;
       pthread_create(&t, NULL, worker, NULL);
       pthread_join(t, NULL);
     }
     mi_collect(true);
     return 0;
   }
   ```
   编译运行：`cc tls_obs.c -o tls_obs -lmimalloc && MIMALLOC_SHOW_STATS=1 ./tls_obs 2>&1 | grep -E "process init|thread|abandon|muv_"`。开 `MIMALLOC_VERBOSE=1` 可再看到 `process init: 0x...` 横幅（在 main 之前打印，承接 u2-l3 的选项时序）。
3. **需要观察的现象**：统计输出中线程相关计数（如 process 段的 threads）为 5（主线程 + 4 个 worker，具体行名以你的构建输出为准）；4 个线程的 theap 已随退出被清空，无泄漏告警。
4. **预期结果**：验证「worker 的默认 theap 在首次 mi_malloc 时建立、在线程 return 后经 pthread destructor 被 abandon/释放」。统计行的确切名称与数值**待本地验证**（依赖构建类型与统计级别，release 默认不输出细粒度统计，建议用 debug 构建）。
5. 可选加深：在 debug 构建下用 `gdb ./tls_obs`，对 `_mi_theap_default_set` 设断点，观察每次命中时的 `theap` 参数与调用栈来源（首次来自 `_mi_thread_init_with_heap`，收尾时来自 `mi_thread_theaps_done` 且参数是 `_mi_theap_empty`）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_mi_theap_cached_set` 必须成对地 incref/decref，而 `_mi_theap_default_set` 不需要？

**答案**：cached 槽保存的 theap 可能属于任意一等堆，该堆可能在线程还缓存着它时被 `mi_heap_delete` 销毁——没有引用计数，槽里就是悬垂指针。default 槽指向的 theap 属于主堆且由本线程的 tld 持有，其生命周期覆盖 default 槽本身（线程退出时 `mi_thread_theaps_done` 先复位槽再释放 theap），天然不会悬垂。

**练习 2**：`_mi_thread_done` 里为什么要先调 `_mi_thread_locals_thread_done()` 释放槽数组，再去处理 theaps？

**答案**：顺序拆解避免两种悬垂：(1) 槽数组本身是动态分配的内存（memid 记账），若不先还，线程退出后无人引用即泄漏；(2) 先清空 `mi_thread_locals`/`mi_slot_fast` 两个根 TLS 变量后，退出路径上任何残留的 `_mi_thread_local_get`（如统计打印）都会安全地拿到 NULL 而不是访问已释放的数组——`mi_thread_local_get_regular` 里那个 `tls==NULL` 检查正是为此预留。

**练习 3**：`_mi_heap_theap` 用 `theap->heap == heap` 做缓存命中判据。什么情况下这个判据会「假命中」或「假未命中」？后果各是什么？

**答案**：不会假命中：theap 只在创建时绑定 heap（`_mi_theap_init` 末尾 release 写入，[src/theap.c:296](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L296)），指针相等即归属正确。假未命中则会发生：线程交替使用两个堆时 cached 槽频繁换人，每次都要多付一次线程局部槽查询加一次引用计数一增一减——这是用局部性换正确性的典型缓存取舍。

## 5. 综合实践

**任务：产出一份「默认 theap 获取路径」平台对照报告。**

1. 通读 [include/mimalloc/prim-tls.h:193-229](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L193-L229) 的模型注释块，为四种模型各写 3 行摘要：取数方式、初始值语义（是否 INITASNULL）、主要风险。
2. 用 4.2.4 的方法画出 Linux 与 Windows 两条完整调用图（从 `mi_malloc` 到最终 load），在每条边上标注访存次数，汇出 \( N \) 值。
3. 补画第三张图：「线程首次分配」时序——空 theap → 慢路径 → `_mi_thread_init_with_heap`（[src/init.c:306-361](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L306-L361)）→ `_mi_theap_default_set` → `_mi_prim_thread_associate_default_theap`（[src/prim/unix/prim.c:1036-1040](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L1036-L1040)），并注明每一步为后续的「线程退出联动」埋了什么。
4. 用 4.4.4 的示例程序实际跑一遍，对照你的时序图核对断点/统计输出。

完成标志：你能不查源码地说出「Linux 上一次 `mi_malloc` 的第一次访存读的是什么、Windows 上多读哪几次、线程退出时谁来收尾」。

## 6. 本讲小结

- `_mi_theap_default()` 内联在每个 `mi_malloc` 里，是快路径的第一次访存、分配成本的固定下限；Linux（LOCAL 模型）下仅 1 次 fs 段寻址 load。
- `prim-tls.h` 维护 LOCAL / PTHREADS / WIN32 / FIXED 四种 TLS 模型，默认按平台选择（Linux→LOCAL、macOS/OpenBSD/Android→PTHREADS、Windows→WIN32），差异被 `MI_THEAP_INITASNULL` 与「借扩展槽当 NULL」这类技巧吸收。
- Windows 路径优先 `TlsAlloc` 的前 64 个 TEB 直接槽（2 次访存），落扩展槽则 4 次；FIXED 模型 1 次但与其他库冲突风险自负。
- 「两级缓存」= `_mi_theap_default`（服务无 heap 参数 API）与 `_mi_theap_cached`（缓存最近用于 `_mi_heap_*` API 的 theap，以 `theap->heap == heap` 判命中）；cached 的写入伴随 theap 引用计数一增一减。
- heap×线程 的二维映射由 threadlocal.c 的自制动态 TLS 承载：每线程一个可增长槽位数组，key 编码「16 位下标 + 48 位版本」，全局原子位图分配槽位，主堆独享 fast 槽，无数量上限。
- 线程退出时，Unix 靠 pthread key destructor（`mi_pthread_done`）、Windows 靠 `DLL_THREAD_DETACH`/FLS 汇入 `_mi_thread_done`：先拆动态槽数组、复位两个槽为空 theap、abandon 页面、按引用计数释放 theap。

## 7. 下一步学习建议

本讲补齐了「线程如何拿到 theap」。下一讲 **u7-l3 一等堆**将把本讲的部件组装成完整 API：`mi_heap_new`/`mi_heap_delete`/`mi_heap_destroy` 与跨线程堆分配——其中每个堆的线程局部槽（本讲 4.3）与 cached 缓存（本讲 4.4）正是其性能支柱，建议结合 `test/test-stress-heaps.c` 阅读并动手做其压测实践。若想先横向扩展，可回看 [src/prim/unix/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c) 中 `_mi_prim_thread_*` 一族的其余接口（线程池判定等），体会 prim 层「一个接口、多种平台语义」的设计（承接 u6-l1）。
