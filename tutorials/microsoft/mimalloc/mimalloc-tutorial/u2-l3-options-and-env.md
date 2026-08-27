# 选项系统：环境变量、mi_option 与运行时调参

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 mimalloc 选项系统的**四种设置途径**（环境变量、`mi_option_set` 编程接口、编译期默认宏、`mi_option_set_default` 库级默认），以及它们之间的**优先级规则**。
2. 解释 `MIMALLOC_` 前缀环境变量是如何被映射到 `mi_option_t` 枚举项上的：名字怎么拼、大小写是否敏感、`TRUE/ON/1GB/100` 这些值分别怎么解析。
3. 理解选项的**三态初始化**（UNINIT / DEFAULTED / INITIALIZED）与惰性求值设计，以及为什么热路径上要使用 `_mi_option_get_fast` 而不是 `mi_option_get`。
4. 掌握 `purge_delay`、`arena_eager_commit` 等关键调优项的确切语义，并能在源码中找到它们的**真实消费点**。
5. 能够分别用环境变量和编程接口复现同一种运行时行为，并解释两者输出为什么不同。

## 2. 前置知识

### 2.1 环境变量与 getenv

环境变量是操作系统给每个进程的一份「字符串键值表」。在 C 里通常用 `getenv("NAME")` 读取，Shell 里用 `NAME=value ./prog` 设置。POSIX 的 `getenv` 是**大小写敏感**的——这对本讲很重要，因为 mimalloc 为了兼容自己实现了一个**大小写不敏感**的查找（见 4.2 节）。

### 2.2 惰性初始化（lazy initialization）

「第一次用到时才初始化」的模式。mimalloc 的每个选项并不在程序一启动就全部读取环境变量，而是**第一次被读取时**才去查。这样做的原因很实际：mimalloc 可能通过 `LD_PRELOAD` 在 **C 运行时还没初始化完**的阶段就被调用（上一讲 u2-l1 讲过的 preload 场景），此时标准 `getenv` 未必可用，必须「先跳过、以后再试」。

### 2.3 表驱动设计

选项系统没有为 47 个选项各写一段解析代码，而是用**一张描述表**（数组）+ **一个通用解析函数**处理所有选项。每个选项在表里占一项，包含默认值、初始化状态、名字。这是 C 项目里非常经典的做法，本讲会反复看到。

### 2.4 与前面讲义的衔接

- u1-l4 中你已经用过 `MIMALLOC_SHOW_STATS=1` 和 `MIMALLOC_VERBOSE=1`，本讲解释这两个变量背后的完整机制。
- u2-l1 中讲过 mimalloc 会在 `main` 之前打印版本横幅——那正是选项系统初始化完成后 `_mi_options_post_init` 干的事。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `include/mimalloc.h` | 公共头：`mi_option_t` 枚举（L463-L520）与全部 `mi_option_*` API 声明（L523-L533） |
| `src/options.c` | 选项系统的全部实现：描述表、环境变量解析、get/set 家族、verbose/错误消息门控 |
| `include/mimalloc/internal.h` | 内部头：`mi_option_init_t` 三态枚举与 `mi_option_desc_t` 描述结构（L456-L468） |
| `src/libc.c` | `_mi_getenv`：把平台原语的结果翻译成 errno 风格返回值（L91-L105） |
| `src/prim/unix/prim.c` | `_mi_prim_getenv`：直接扫 `environ` 数组、大小写不敏感匹配（L888-L904） |
| `src/init.c` | 选项初始化的**调用时机**：进程初始化序列（L536-L546、L505-L513） |
| `src/arena.c` / `src/os.c` | `purge_delay` 的真实消费点（arena.c L2242-L2252、os.c L657-L667） |
| `readme.md` | 官方文档中的 Environment Options 一节（L348-L404） |

阅读建议：本讲以 `src/options.c` 为主线，其他文件都是它的「上下游」。

## 4. 核心概念与源码讲解

### 4.1 选项全景：mi_option_t 枚举与选项描述表

#### 4.1.1 概念说明

mimalloc 有大量可调行为：何时把空闲内存还给 OS、是否用大页、统计是否打印……这些都抽象成「选项」。每个选项有三要素：

- **名字**：如 `purge_delay`，加上 `mimalloc_` 前缀后就是环境变量名 `MIMALLOC_PURGE_DELAY`。
- **默认值**：写在描述表里，部分默认值可被编译期宏覆盖。
- **运行时值**：一个 `long`，可被环境变量或 `mi_option_set` 改写。

选项在公共 API 中按**稳定性**分两档：

- **稳定选项（stable）**：只有 3 个——`show_errors`、`show_stats`、`verbose`。接口语义长期保证。
- **高级选项（advanced）**：其余全部，其中 9 个名字带 `deprecated_` 前缀的已废弃（v3 相对 v1/v2 改名后保留占位），注释里明确写着「experimental and not all combinations are allowed」。

v3.5 的完整清单是 **3 个稳定 + 44 个高级 = 47 个选项**，全部定义在一个枚举里。

#### 4.1.2 核心流程

选项的定义与实现分布在三处，靠**枚举顺序**粘合：

```text
include/mimalloc.h          src/options.c                     include/mimalloc/internal.h
┌──────────────────┐   ┌────────────────────────┐   ┌─────────────────────────┐
│ mi_option_t 枚举  │   │ mi_options[] 描述表     │   │ mi_option_desc_t 结构    │
│ (顺序即下标)      │←──│ 第 i 项必须对应枚举 i    │──→│ value/init/option/name/ │
│ stable → advanced│   │ 默认值 + MI_OPTION 宏   │   │ legacy_name 五个字段     │
└──────────────────┘   └────────────────────────┘   └─────────────────────────┘
```

关键不变式：**枚举成员的声明顺序必须与 `mi_options[]` 表的初始化顺序一一对应**。`mi_option_get` 内部用 `mi_assert(desc->option == option)` 在调试构建下检查这一点。

#### 4.1.3 源码精读

**① 枚举：稳定选项与高级选项的分界**（[include/mimalloc.h:463-520](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L463-L520)）：

```c
typedef enum mi_option_e {
  // stable options
  mi_option_show_errors,                // print error messages
  mi_option_show_stats,                 // print statistics on termination
  mi_option_verbose,                    // print verbose messages
  // advanced options
  mi_option_deprecated_eager_commit,
  mi_option_arena_eager_commit,         // eager commit arenas? Use 2 to enable just on overcommit systems (=2)
  ...
  mi_option_purge_delay,                // memory purging is delayed by N milli seconds (=10)
  ...
  _mi_option_last,
  // legacy option names
  mi_option_large_os_pages = mi_option_allow_large_os_pages,
  mi_option_reset_delay = mi_option_purge_delay,
  ...
} mi_option_t;
```

要点：

- 前三个是稳定选项；`_mi_option_last`（[L513](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L513)）是哨兵，既表示选项总数，也是遍历描述表的循环上界。
- L514-L519 的 **legacy 别名**：v3 改名后，旧代码里的 `mi_option_reset_delay` 通过 `= mi_option_purge_delay` 继续可用——枚举别名不占新下标。

**② 描述表：一行一个选项**（[src/options.c:112-178](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L112-L178)）：

```c
static mi_option_desc_t mi_options[_mi_option_last] =
{
  // stable options
#if MI_DEBUG || defined(MI_SHOW_ERRORS)
  { 1, MI_OPTION_UNINIT, MI_OPTION(show_errors) },
#else
  { 0, MI_OPTION_UNINIT, MI_OPTION(show_errors) },
#endif
  { 0, MI_OPTION_UNINIT, MI_OPTION(show_stats) },
  { MI_DEFAULT_VERBOSE, MI_OPTION_UNINIT, MI_OPTION(verbose) },
  ...
  { 1000,MI_OPTION_UNINIT, MI_OPTION_LEGACY(purge_delay,reset_delay) },  // purge delay in milli-seconds
  ...
};
```

每个初始化项的字段顺序是 `{ 默认值, 初始化状态, MI_OPTION(名字) }`。两个细节：

- `show_errors` 的默认值**取决于构建类型**：debug 构建（或定义了 `MI_SHOW_ERRORS`）默认开，release 默认关——所以 release 下默认看不到 mimalloc 的错误消息，除非设置 `MIMALLOC_SHOW_ERRORS=1`。
- `purge_delay` 默认值 1000 毫秒（readme 中 v3 默认值说明见 [readme.md:364-368](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L364-L368)），并用 `MI_OPTION_LEGACY` 登记了旧名 `reset_delay`。

**③ 两个填充宏**（[src/options.c:33-34](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L33-L34)）：

```c
#define MI_OPTION(opt)                  mi_option_##opt, #opt, NULL
#define MI_OPTION_LEGACY(opt,legacy)    mi_option_##opt, #opt, #legacy
```

`mi_option_##opt` 把记号拼成枚举值，`#opt` 字符串化成名字（如 `"purge_delay"`），第三个字段是可选旧名。一行宏同时填满描述结构的后三个字段。

**④ 描述结构：值的容器**（[include/mimalloc/internal.h:456-468](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L456-L468)）：

```c
typedef enum mi_option_init_e {
  MI_OPTION_UNINIT,       // not yet initialized
  MI_OPTION_DEFAULTED,    // not found in the environment, use default value
  MI_OPTION_INITIALIZED   // found in environment or set explicitly
} mi_option_init_t;

typedef struct mi_option_desc_s {
  long              value;  // the value
  mi_option_init_t  init;   // is it initialized yet? (from the environment)
  mi_option_t       option; // for debugging: the option index should match the option
  const char*       name;   // option name without `mimalloc_` prefix
  const char*       legacy_name; // potential legacy option name
} mi_option_desc_t;
```

三态 `init` 是整个系统的核心状态机：**UNINIT（还没查过环境）→ DEFAULTED（查了但环境里没有）或 INITIALIZED（环境变量或程序显式设置）**。4.3 节会看到所有 get/set 都围绕这个状态机转。

**⑤ 编译期默认值：第四种设置途径**（[src/options.c:36-48](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L36-L48)）：

```c
// Some options can be set at build time for statically linked libraries
// (use `-DMI_EXTRA_CPPDEFS="opt1=val1;opt2=val2"`)
//
// This is useful if we cannot pass them as environment variables
// (and setting them programmatically would be too late)

#ifndef MI_DEFAULT_VERBOSE
#define MI_DEFAULT_VERBOSE 0
#endif

#ifndef MI_DEFAULT_ARENA_EAGER_COMMIT
#define MI_DEFAULT_ARENA_EAGER_COMMIT 2
#endif
```

静态链接进别的程序时，环境变量可能被宿主环境吞掉、编程接口又可能「太晚了」（分配已发生），于是 cmake 提供 `-DMI_EXTRA_CPPDEFS` 把默认值烧进二进制。表里凡是引用 `MI_DEFAULT_*` 宏的选项都支持这一途径，例如 `arena_eager_commit` 的默认值 2 表示「仅在 Linux 这类 overcommit 系统上启用 eager commit」（语义见 [readme.md:358-363](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L358-L363)）。

#### 4.1.4 代码实践

**实践目标**：亲手导出全部 47 个选项的当前值，建立「选项=名字+数值」的直观印象。

**操作步骤**：

1. 写一个最小程序（**示例代码**，非项目原有文件）：

```c
/* options-dump.c: 打印 mimalloc 全部选项 */
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  mi_malloc(16);            /* 触发库初始化（非必须，print 内部会自行初始化） */
  mi_options_print();       /* 打印版本 + 全部选项 + 构建配置 */
  mi_free(mi_malloc(32));
  return 0;
}
```

2. 用 release 构建的静态库编译（沿用 u1-l2 的构建产物）：

```bash
gcc -I include options-dump.c out/release/libmimalloc.a -lpthread -lm -o options-dump
./options-dump
```

3. 再跑一次带环境变量的版本，对比差异：

```bash
MIMALLOC_PURGE_DELAY=0 MIMALLOC_ARENA_EAGER_COMMIT=1 ./options-dump | grep -E "purge_delay|arena_eager_commit"
```

**需要观察的现象**：

- 输出第一行是版本号（`v3.5.0...`），随后是 47 行形如 `option 'purge_delay': 1000` 的清单，最后是 `debug level/secure level` 等构建配置段（打印逻辑在 4.4 节分析的 [src/options.c:214-259](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L214-L259)）。
- 第 3 步中 `purge_delay` 的值应从 1000 变为 0。
- 带 KiB 单位的选项（如 `arena_reserve`）行尾会多一个 `KiB` 后缀。

**预期结果**：能拿到一份完整的选项快照，且环境变量改变对应行数值。具体打印格式随版本可能微调，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mi_option_t` 枚举里要放一个 `_mi_option_last`？
**答案**：它充当数组长度与遍历哨兵——`src/options.c` 用 `mi_options[_mi_option_last]` 定义描述表，`_mi_options_init` 等函数用它做循环上界，无需手写「选项个数」常量，新增选项时自动同步。

**练习 2**：`mi_option_reset_delay` 和 `mi_option_purge_delay` 是两个不同选项吗？
**答案**：不是。前者是后者的枚举别名（[include/mimalloc.h:518](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L518)），值相同，只是让 v1/v2 时代的源码在 v3 下继续编译。而**环境变量**层面的旧名兼容走的是另一条路：描述表里的 `legacy_name` 字段（见 4.2 节）。

**练习 3**：数一数描述表里默认值不为 0 的稳定选项有几个？分别是哪些？
**答案**：视构建而定。`show_stats` 默认 0；`verbose` 默认 `MI_DEFAULT_VERBOSE`=0；`show_errors` 在 debug 构建默认 1、release 默认 0（[src/options.c:115-119](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L115-L119)）。所以 release 下三个稳定选项默认全关，debug 下只有 `show_errors` 开。

### 4.2 环境变量 → 选项值：mi_option_init 的解析规则

#### 4.2.1 概念说明

每个选项第一次被读取时，`mi_option_init` 会拿它的名字去环境变量表里找。这一节回答四个问题：

1. 环境变量名怎么拼？——`mimalloc_` + 选项名（小写），匹配**大小写不敏感**。
2. 找不到新名怎么办？——再找一次旧名（legacy_name），命中则打弃用警告。
3. 值怎么解析？——先查布尔词表（`1/TRUE/YES/ON`、`0/FALSE/NO/OFF`），再用 `strtol` 解析数字；带 KiB 单位的选项支持 `K/M/G/T` 后缀。
4. 值不合法怎么办？——保持默认值并打警告；`verbose` 本身还要特殊处理（否则你永远看不到这条警告）。

#### 4.2.2 核心流程

```text
mi_option_init(desc)
  │
  ├─ 拼名："mimalloc_" + desc->name        （如 "mimalloc_purge_delay"）
  ├─ _mi_getenv ──→ _mi_prim_getenv        （扫 environ，大小写不敏感）
  │     ├─ 找到 → 返回 0（成功），值在 s[]
  │     ├─ 没找到（ENOENT）且有旧名 → 用 "mimalloc_" + legacy_name 再找一次
  │     │        └─ 命中 → 弃用警告
  │     └─ 暂时不可用（EAGAIN，preload 阶段）→ 保持 UNINIT，下次再试
  │
  ├─ 值转大写后匹配布尔词表:
  │     ""/1/TRUE/YES/ON  → value=1
  │     0/FALSE/NO/OFF    → value=0
  ├─ 否则 strtol 解析数字:
  │     ├─ 是 KiB 型选项 → 支持 K/M/G/T(±IB/B) 后缀；无后缀则向上取整到 KiB
  │     └─ 解析成功 → mi_option_set(option, value)
  └─ 解析失败 → 保持默认值 + 警告 "invalid value"
        └─ 特例：verbose 自身值非法时，短暂置 1 打完警告再还原为 0
```

#### 4.2.3 源码精读

**① 拼名、查找与旧名回退**（[src/options.c:623-637](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L623-L637)）：

```c
static void mi_option_init(mi_option_desc_t* desc) {
  // Read option value from the environment
  char s[64 + 1];
  char buf[64+1];
  _mi_strlcpy(buf, "mimalloc_", sizeof(buf));
  _mi_strlcat(buf, desc->name, sizeof(buf));
  int err = _mi_getenv(buf, s, sizeof(s));
  if (err==ENOENT && desc->legacy_name != NULL) {
    _mi_strlcpy(buf, "mimalloc_", sizeof(buf));
    _mi_strlcat(buf, desc->legacy_name, sizeof(buf));
    err = _mi_getenv(buf, s, sizeof(s));
    if (err==0) {
      _mi_warning_message("environment option \"mimalloc_%s\" is deprecated -- use \"mimalloc_%s\" instead.\n", desc->legacy_name, desc->name);
    }
  }
```

名字是小写拼出来的（`mimalloc_purge_delay`），但你看下面的平台实现，匹配是大小写不敏感的，所以 `MIMALLOC_PURGE_DELAY` 一样有效。

**② 平台层：为什么不用标准 getenv**（[src/prim/unix/prim.c:874-904](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L874-L904)）：

```c
// On Posix systems use `environ` to access environment variables
// even before the C runtime is initialized.
...
int _mi_prim_getenv(const char* name, char* result, size_t result_size) {
  ...
  char** env = mi_get_environ();
  ...
  for (int i = 0; i < 10000 && env[i] != NULL; i++) {
    const char* s = env[i];
    if (_mi_strnicmp(name, s, len) == 0 && s[len] == '=') { // case insensitive
      ...
      return 1;   // success
    }
  }
  return 0; // not found
}
```

两个设计点：直接遍历全局 `environ` 数组（macOS 用 `_NSGetEnviron`），从而在 C 运行时初始化完成前（preload 阶段）也能读环境；`_mi_strnicmp` 让匹配**大小写不敏感**——这就是 readme 全用大写 `MIMALLOC_PURGE_DELAY` 而源码拼的是小写 `mimalloc_purge_delay` 却都能工作的原因。

**③ 错误码翻译**（[src/libc.c:91-105](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/libc.c#L91-L105)）：

```c
int _mi_getenv(const char* name, char* result, size_t result_size) {
  ...
  const int res = _mi_prim_getenv(name,result,result_size);
  return (res > 0 ? 0 : (res == 0 ? ENOENT : EAGAIN));
}
```

三态返回：成功→`0`、不存在→`ENOENT`、暂时读不了→`EAGAIN`。`EAGAIN` 驱动了惰性重试：见 [src/options.c:692-696](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L692-L696) 的收尾——

```c
  else if (err==ENOENT) {
    desc->init = MI_OPTION_DEFAULTED;
  }
  // and on another error, keep unitialized to try again later (can happen during preloading if getenv is not available)
```

`ENOENT` 把状态推进到 DEFAULTED（用默认值，不再查）；其他错误保持 UNINIT，等下次有人 `mi_option_get` 时再试。

**④ 布尔词表与数字解析**（[src/options.c:639-671](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L639-L671)）：

```c
  if (err==0) {
    size_t len = _mi_strnlen(s, sizeof(buf) - 1);
    for (size_t i = 0; i < len; i++) { buf[i] = _mi_toupper(s[i]); }   // 值统一转大写
    ...
    if (buf[0] == 0 || _mi_streq(buf,"1") || _mi_streq(buf,"TRUE") || _mi_streq(buf,"YES") || _mi_streq(buf,"ON")) {
      desc->value = 1; ...
    }
    else if (_mi_streq(buf,"0") || _mi_streq(buf,"FALSE") || _mi_streq(buf,"NO") || _mi_streq(buf,"OFF")) {
      desc->value = 0; ...
    }
    else {
      char* end = buf;
      errno = 0;
      long value = strtol(buf, &end, 10);
      if (errno==0 && mi_option_has_size_in_kib(desc->option)) {
        // this option is interpreted in KiB to prevent overflow of `long` for large allocations
        ...
        if (*end == 'K') { end++; }
        else if (*end == 'M') { overflow = mi_mul_overflow(size,MI_KiB,&size); end++; }
        else if (*end == 'G') { overflow = mi_mul_overflow(size,MI_MiB,&size); end++; }
        else if (*end == 'T') { overflow = mi_mul_overflow(size,MI_GiB,&size); end++; }
        else { size = (size + MI_KiB - 1) / MI_KiB; }   // 无后缀：按字节向上取整到 KiB
        ...
      }
      if (errno==0 && *end == 0) {
        mi_option_set(desc->option, value);
      }
```

注意三个坑：

- **空字符串算 1**（`buf[0] == 0` 在真分支里），即 `MIMALLOC_VERBOSE=` 等于 `=1`。
- 只有 4 个「KiB 型」选项走后缀逻辑，由 [src/options.c:182-185](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L182-L185) 判定：`reserve_os_memory`、`arena_reserve`、`minimal_purge_size`、`arena_max_object_size`。它们**内部值一律以 KiB 计**，注释解释了原因：Windows 上 `long` 只有 32 位，存字节数会溢出。所以 `MIMALLOC_ARENA_RESERVE=1G` 合法，而 `MIMALLOC_PURGE_DELAY=1G` 会因 `*end != 0` 被判为非法值。
- 解析成功走的是 `mi_option_set`（把状态置为 INITIALIZED），而不是直接赋值——和编程接口共用同一条写入路径。

**⑤ 无效值与 verbose 特例**（[src/options.c:672-688](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L672-L688)）：

```c
      else {
        // set `init` first to avoid recursion through _mi_warning_message on mimalloc_verbose.
        desc->init = MI_OPTION_DEFAULTED;
        if (desc->option == mi_option_verbose && desc->value == 0) {
          // if the 'mimalloc_verbose' env var has a bogus value we'd never know
          // (since the value defaults to 'off') so in that case briefly enable verbose
          desc->value = 1;
          _mi_warning_message("environment option mimalloc_%s has an invalid value.\n", desc->name);
          desc->value = 0;
        }
```

如果 `MIMALLOC_VERBOSE=abc`，警告本身会被「verbose 未开启」的门控吞掉（见 4.4 节消息门控），于是这里**先把 verbose 临时置 1、打完警告再还原**——一个很贴心的小补丁。同时注释提醒：必须先设 `init` 再发警告，否则警告会经 `_mi_warning_message → mi_option_is_enabled(verbose) → mi_option_get → mi_option_init` 递归回来。

#### 4.2.4 代码实践

**实践目标**：验证大小写不敏感、布尔词表、无效值警告三件事。

**操作步骤**（复用 4.1.4 的 `options-dump`，**示例代码**）：

```bash
# ① 全小写变量名 + 单词值
mimalloc_purge_delay=0 ./options-dump | grep purge_delay

# ② 布尔词表：ON/YES 与 OFF 应等价于 1/0
MIMALLOC_VERBOSE=ON ./options-dump | head -3
MIMALLOC_SHOW_STATS=NO ./options-dump | grep show_stats

# ③ 无效值：非数字
MIMALLOC_PURGE_DELAY=abc ./options-dump 2>&1 | head -5

# ④ verbose 自身非法
MIMALLOC_VERBOSE=abc ./options-dump 2>&1 | head -3
```

**需要观察的现象**：

- ① 全小写变量名同样生效（证明 `_mi_strnicmp` 匹配），`purge_delay` 显示 0。
- ② `ON` 能打开 verbose（输出大量 `mimalloc:` 前缀消息）；`NO` 使 `show_stats` 保持 0。
- ③ stderr 出现 `mimalloc: warning: environment option mimalloc_purge_delay has an invalid value.`——前提是警告通道开着：release 构建默认 `show_errors=0`，可能什么都看不到，需加 `MIMALLOC_SHOW_ERRORS=1`。
- ④ 即使 verbose 值非法，警告仍能打印出来（特例代码生效）。

**预期结果**：四条全部符合上述描述。release 构建下 ③④ 的警告是否可见取决于 `show_errors`，若不可见请叠加 `MIMALLOC_SHOW_ERRORS=1` 重试；具体警告文案**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`MIMALLOC_ARENA_RESERVE=64M` 与 `=65536` 与 `=67108864` 三种写法最终存进描述表的 `value` 分别是多少？
**答案**：都是 65536（KiB，即 64MiB）。`64M` 走 `M` 后缀分支乘 `MI_KiB` 换算成 KiB；`65536` 被当作 KiB 直接用；`67108864`（字节）走「无后缀向上取整到 KiB」分支 `(67108864 + 1023) / 1024 = 65536`。读取时再由 `mi_option_get_size` 乘回 `MI_KiB`（见 4.3 节）。

**练习 2**：为什么 `mi_option_init` 里解析数字成功后要调用 `mi_option_set` 而不是直接 `desc->value = value`？
**答案**：`mi_option_set` 除了赋值还会把 `init` 置为 `MI_OPTION_INITIALIZED`，并处理 `guarded_min/guarded_max` 的联动约束（[src/options.c:302-316](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L302-L316)）。走同一条路径保证环境变量和编程接口行为完全一致。

**练习 3**：在 preload 场景下 `_mi_prim_getenv` 返回 -1（暂不可用），选项系统会怎样？
**答案**：`_mi_getenv` 把它翻译成 `EAGAIN`，`mi_option_init` 对非 `ENOENT` 的错误**保持 UNINIT 不动**（[src/options.c:695](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L695)），下次 `mi_option_get` 触碰该选项时再重试。这就是三态设计存在的根本原因。

### 4.3 编程接口与惰性初始化：get/set 家族与优先级

#### 4.3.1 概念说明

公共 API 一共 11 个函数（声明在 [include/mimalloc.h:523-533](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L523-L533)），按用途分三组：

| 组 | 函数 | 语义 |
|---|---|---|
| 布尔组 | `mi_option_is_enabled` / `mi_option_enable` / `mi_option_disable` / `mi_option_set_enabled` / `mi_option_set_enabled_default` | 把选项当开关（值非 0 即启用） |
| 数值组 | `mi_option_get` / `mi_option_get_clamp` / `mi_option_get_size` / `mi_option_set` / `mi_option_set_default` | 选项是 `long` 数值 |
| 内部组 | `_mi_option_get_fast`（internal.h 暴露） | 热路径专用，跳过惰性检查 |

理解这一节的关键是**三条读取路径 + 一套优先级规则**：

优先级（从高到低）：

1. `mi_option_set`（程序显式设置，包括环境变量解析成功时内部调用的那次）——无条件覆盖，置 INITIALIZED。
2. 环境变量（通过 `mi_option_init` → `mi_option_set` 生效）。
3. `mi_option_set_default`——**仅当选项还不是 INITIALIZED 时**才生效，即它改不动被环境变量或 `mi_option_set` 设置过的选项。
4. 描述表默认值（可被编译期 `MI_DEFAULT_*` 宏覆盖）。

还有一个工程细节：热路径上的读取用 `_mi_option_get_fast`，它**不检查**初始化状态、直接返回 `desc->value`。这之所以安全，是因为进程初始化时 `_mi_options_init` 已经把**所有**选项强制初始化过一遍（4.4 节）。

#### 4.3.2 核心流程

```text
读取：
  mi_option_get(opt)
    └─ desc->init == UNINIT ?  ──是──→ mi_option_init(desc)   （查环境，可能重试留 UNINIT）
                              ──否──→ 直接返回 desc->value

  _mi_option_get_fast(opt)     ← 热路径（free.c / page.c）
    └─ 无条件返回 desc->value   （前提：进程 init 已把它初始化好）

写入：
  mi_option_set(opt, v)        → value=v; init=INITIALIZED（无条件覆盖）
  mi_option_set_default(opt,v) → 仅当 init != INITIALIZED 时 value=v（不覆盖 env/显式设置）
```

并发说明：[src/options.c:25-30](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L25-L30) 的注释写明——选项可被多线程并发初始化，这种「初始化数据竞争」是良性的，因为所有线程最终会解析出同一个值。

#### 4.3.3 源码精读

**① mi_option_get：惰性求值**（[src/options.c:275-284](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L275-L284)）：

```c
mi_decl_nodiscard long mi_option_get(mi_option_t option) {
  mi_assert(option >= 0 && option < _mi_option_last);
  if (option < 0 || option >= _mi_option_last) return 0;
  mi_option_desc_t* desc = &mi_options[option];
  mi_assert(desc->option == option);  // index should match the option
  if mi_unlikely(desc->init == MI_OPTION_UNINIT) {
    mi_option_init(desc);
  }
  return desc->value;
}
```

`mi_unlikely` 提示编译器：初始化分支几乎不会执行（多数调用发生在进程 init 之后），保证快路径紧凑。`mi_assert` 那行同时是「枚举顺序=表顺序」不变式的守卫。

**② _mi_option_get_fast：热路径版本**（[src/options.c:266-272](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L266-L272)）：

```c
long _mi_option_get_fast(mi_option_t option) {
  mi_assert(option >= 0 && option < _mi_option_last);
  mi_option_desc_t* desc = &mi_options[option];
  mi_assert(desc->option == option);  // index should match the option
  //mi_assert(desc->init != MI_OPTION_UNINIT);
  return desc->value;
}
```

连「init 是否完成」的断言都被注释掉了——纯粹一次数组下标 + 一次内存读。真实调用点举例：释放路径读 `page_reclaim_on_free`（[src/free.c:500](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L500)）、分配慢路径读 `page_max_candidates`（[src/page.c:805](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L805)）。这些是每次 free/malloc 都可能走到的代码，多一次分支判断都是开销。

**③ mi_option_set 与联动约束**（[src/options.c:302-316](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L302-L316)）：

```c
void mi_option_set(mi_option_t option, long value) {
  ...
  desc->value = value;
  desc->init = MI_OPTION_INITIALIZED;
  // ensure min/max range; be careful to not recurse.
  if (desc->option == mi_option_guarded_min && _mi_option_get_fast(mi_option_guarded_max) < value) {
    mi_option_set(mi_option_guarded_max, value);
  }
  else if (desc->option == mi_option_guarded_max && _mi_option_get_fast(mi_option_guarded_min) > value) {
    mi_option_set(mi_option_guarded_min, value);
  }
}
```

唯一带「约束传播」的选项对：guarded 采样的 min/max 必须有序，设小了另一个会跟着抬。注意它读另一端用的是 `_mi_option_get_fast`——避免在 set 过程中触发 `mi_option_init` 的重入。

**④ mi_option_set_default：让位给环境变量**（[src/options.c:318-325](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L318-L325)）：

```c
void mi_option_set_default(mi_option_t option, long value) {
  ...
  mi_option_desc_t* desc = &mi_options[option];
  if (desc->init != MI_OPTION_INITIALIZED) {
    desc->value = value;
  }
}
```

这是给「库作者/嵌入方」用的：想改默认行为，但**尊重用户显式传的环境变量**。与之对照，`mi_option_set` 无条件覆盖。

**⑤ 辅助读取**（[src/options.c:286-300](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L286-L300)）：

```c
mi_decl_nodiscard long mi_option_get_clamp(mi_option_t option, long min, long max) {
  long x = mi_option_get(option);
  return (x < min ? min : (x > max ? max : x));
}

mi_decl_nodiscard size_t mi_option_get_size(mi_option_t option) {
  const long x = mi_option_get(option);
  size_t size = (x < 0 ? 0 : (size_t)x);
  if (mi_option_has_size_in_kib(option)) {
    if (mi_mul_overflow(size, MI_KiB, &size)) {
      size = MI_MAX_ALLOC_SIZE;
    }
  }
  return size;
}
```

`get_clamp` 用于把用户给的值压进安全区间（如 [src/init.c:567](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L567) 把 `reserve_huge_os_pages` 压到 0..128K）；`get_size` 是 KiB 型选项的配套读取——内部存 KiB、对外给字节，乘法溢出时钳到 `MI_MAX_ALLOC_SIZE`。

**⑥ 布尔组只是语法糖**（[src/options.c:327-345](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L327-L345)）：

```c
mi_decl_nodiscard bool mi_option_is_enabled(mi_option_t option) {
  return (mi_option_get(option) != 0);
}
void mi_option_enable(mi_option_t option)  { mi_option_set_enabled(option,true);  }
void mi_option_disable(mi_option_t option) { mi_option_set_enabled(option,false); }
```

「启用」就是「值非 0」，没有独立的布尔存储。所以 `mi_option_enable(mi_option_verbose)` 与 `mi_option_set(mi_option_verbose, 1)` 完全等价。

#### 4.3.4 代码实践

**实践目标**：验证优先级规则——`mi_option_set_default` 争不过环境变量，`mi_option_set` 可以。

**操作步骤**（**示例代码**）：

```c
/* options-priority.c: 验证 set / set_default / env 三者优先级 */
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  /* set_default：若环境变量已设置则无效 */
  mi_option_set_default(mi_option_purge_delay, 7);
  printf("after set_default(7)      : purge_delay = %ld\n",
         mi_option_get(mi_option_purge_delay));

  /* set：无条件覆盖 */
  mi_option_set(mi_option_purge_delay, 0);
  printf("after set(0)              : purge_delay = %ld\n",
         mi_option_get(mi_option_purge_delay));
  return 0;
}
```

```bash
gcc -I include options-priority.c out/release/libmimalloc.a -lpthread -lm -o options-priority

# 场景 A：不设环境变量
./options-priority
# 场景 B：设环境变量
MIMALLOC_PURGE_DELAY=100 ./options-priority
```

**需要观察的现象**：

- 场景 A 第一行输出 7（环境没设，`set_default` 生效）；场景 B 第一行输出 100（环境变量已把 init 置为 INITIALIZED，`set_default` 被跳过）。
- 两个场景第二行都是 0（`mi_option_set` 无条件覆盖）。

**预期结果**：如上所述。注意场景 B 里「环境变量先于 main 生效」依赖进程初始化在 `main` 前完成（4.4 节）；若你在 `main` 最开头就打印也观察不到中间态，因为初始化发生在库加载时。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`mi_option_get` 与 `_mi_option_get_fast` 的唯一行为差异是什么？为什么 free.c 里必须用后者？
**答案**：差异是后者不检查（也不触发）惰性初始化，只是裸读 `desc->value`。free.c 的释放路径每次 `mi_free` 都执行，若用 `mi_option_get`，每次都要多一次 `init` 状态判断分支；而进程初始化时 `_mi_options_init` 已保证所有选项离开 UNINIT 态，所以省掉检查是安全的。

**练习 2**：某嵌入方希望「默认打开 show_stats，但允许用户用环境变量关掉」。应该调用哪个函数？
**答案**：`mi_option_set_enabled_default(mi_option_show_stats, true)`（内部即 `mi_option_set_default`，见 [src/options.c:335-337](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L335-L337)）。它只在选项未被显式设置时生效：用户没设环境变量→默认开；用户设了 `MIMALLOC_SHOW_STATS=0`→尊重用户。若用 `mi_option_enable` 则用户的环境变量也会被覆盖。

**练习 3**：多线程同时第一次读同一个未初始化选项，会发生什么？
**答案**：两个线程可能同时进入 `mi_option_init` 各自解析一遍环境变量（数据竞争），但正如 [src/options.c:25-30](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L25-L30) 注释所说，这是**良性竞争**：两个线程会解析出相同的值并写回同一位置，最终状态一致。选项系统因此完全不需要锁。

### 4.4 关键调优项与真实消费点：purge_delay、初始化时序与消息门控

#### 4.4.1 概念说明

知道选项怎么解析还不够，调优必须落到「谁在读它、什么时候读」。本节串起三件事：

1. **初始化时序**：`_mi_options_init`（强制初始化全部选项）和 `_mi_options_post_init`（verbose 时打印选项表）在进程生命周期中的确切位置——这决定了「环境变量方式」和「编程接口方式」的输出差异。
2. **purge_delay 的语义与消费点**：它控制 mimalloc 何时把空闲内存「purge」（归还物理页）给 OS，是长时运行服务最重要的一个调优项。
3. **verbose 的分级**：`verbose=1` 打普通消息，`verbose>=2` 才打 trace 级消息。

#### 4.4.2 核心流程

进程启动时的选项相关时序（结合 u2-l1 讲过的「版本横幅在 main 前打印」）：

```text
库被加载（LD_PRELOAD 或链接期构造函数）
  └─ _mi_auto_process_init                 (init.c:506)
       └─ mi_process_init
            └─ mi_process_init_once        (init.c:537)
                 ├─ _mi_detect_cpu_features
                 ├─ _mi_options_init       ← ①强制初始化全部 47 个选项（读环境）
                 ├─ _mi_stats_init / _mi_os_init / mi_heap_main_init / _mi_page_map_init
                 └─ mi_thread_init ...
       └─ _mi_options_post_init            ← ②挂接 stderr；若 verbose 开启 → mi_options_print() 全表打印
main()
  └─ 你调用的 mi_option_set / mi_option_enable   ← ③晚于 ①②，只能影响之后的读取
```

`purge_delay` 的判定链（消费者视角）：

```text
某页变空闲，准备 purge
  └─ _mi_os_purge_ex (os.c:659)
       └─ mi_option_get(mi_option_purge_delay) < 0 ?  → 是：直接返回，彻底不 purge
arena 级 purge 延迟
  └─ mi_arena_purge_delay (arena.c:2242)
       ├─ delay = purge_delay × arena_purge_mult     （默认 1000 × 4 = 4000ms）
       ├─ delay<0 或 mult<0 → -1（不 purge）
       ├─ delay==0 或 mult==0 → 0（立即 purge）
       └─ 否则返回乘积（溢出则退回 delay 本身）
```

#### 4.4.3 源码精读

**① 谁在进程初始化时调用选项系统**（[src/init.c:536-546](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L536-L546)）：

```c
static void mi_process_init_once(void) {
  ...
  _mi_verbose_message("process init: 0x%zx\n", _mi_thread_id());

  _mi_detect_cpu_features();
  _mi_options_init();        // read environment (if possible)
  _mi_stats_init();          // start timer
  _mi_os_init();             // primitive dependent
  ...
```

注意顺序：`_mi_verbose_message` 排在 `_mi_options_init` **之前**——它内部读 `verbose` 选项时才发现是 UNINIT，于是先走惰性初始化解析环境变量。也就是说惰性机制保证了「谁先用谁先初始化」，而 `_mi_options_init` 只是把这个过程对所有选项补齐。

**② _mi_options_init：把每个选项都碰一遍**（[src/options.c:187-203](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L187-L203)）：

```c
void _mi_options_init(void) {
  // called on process load
  for(int i = 0; i < _mi_option_last; i++ ) {
    mi_option_t option = (mi_option_t)i;
    long l = mi_option_get(option); MI_UNUSED(l); // initialize
  }
  mi_max_error_count = mi_option_get(mi_option_max_errors);
  mi_max_warning_count = mi_option_get(mi_option_max_warnings);
  ...
```

循环体什么都不做，纯粹为了让每个选项经过 `mi_option_get` 完成初始化。此后 `_mi_option_get_fast` 的裸读才安全。随后 error/warning 的条数上限也被缓存到静态变量（这是 4.2 节警告能被限流的原因）。

**③ _mi_options_post_init 与 verbose 全表打印**（[src/options.c:206-209](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L206-L209)，调用点 [src/init.c:505-513](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L505-L513)）：

```c
// called at actual process load, it should be safe to print now
void _mi_options_post_init(void) {
  mi_add_stderr_output(); // now it safe to use stderr for output
  if (mi_option_is_enabled(mi_option_verbose)) { mi_options_print(); }
}
```

**这正是两种调参方式输出不同的根源**：环境变量 `MIMALLOC_VERBOSE=1` 在进程加载时已生效，于是这里把版本号 + 47 个选项值 + 构建配置全部打印出来（打印函数为 [src/options.c:214-264](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L214-L264) 的 `mi_options_print_out`，公共声明在 [include/mimalloc.h:195-197](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L195-L197)）；而你在 `main` 里 `mi_option_enable(mi_option_verbose)` 时这一步早已过去，**不会再打印选项表**，只影响之后的零散 verbose 消息。

**④ purge_delay 消费点一：OS 层总开关**（[src/os.c:657-667](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L657-L667)）：

```c
bool _mi_os_purge_ex(...)
{
  if (mi_option_get(mi_option_purge_delay) < 0) return false;  // is purging allowed?
  ...
  else if (mi_option_is_enabled(mi_option_purge_decommits) &&   // should decommit?
           !_mi_preloading())
  {
    bool needs_recommit = true;
    mi_os_decommit_ex(subproc, p, size, &needs_recommit, stat_size);
    return needs_recommit;
  }
```

`purge_delay < 0`（即 `MIMALLOC_PURGE_DELAY=-1`）在总入口就直接短路——**完全关闭 purge**。`purge_decommits`（默认 1）再决定 purge 的实现方式：decommit（Linux 上 `MADV_DONTNEED`，RSS 立降）还是 reset（一般 `MADV_FREE`，RSS 延迟下降），语义详见 [readme.md:364-373](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L364-L373)。

**⑤ purge_delay 消费点二：arena 级延迟放大**（[src/arena.c:2242-2252](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2242-L2252)）：

```c
static long mi_arena_purge_delay(void) {
  // <0 = no purging allowed, 0=immediate purging, >0=milli-second delay
  const long delay = mi_option_get(mi_option_purge_delay);
  const long mult  = mi_option_get(mi_option_arena_purge_mult);
  if (delay<0 || mult<0)   { return -1; }
  if (delay==0 || mult==0) { return 0; }
  size_t total;
  if (mi_mul_overflow((size_t)delay, (size_t)mult, &total)) { return delay; }
  if (total > LONG_MAX) { return delay; }
  return (long)total;
}
```

arena（mimalloc 的大内存区，单元六细讲）的 purge 延迟 = `purge_delay × arena_purge_mult`（默认 1000×4=4000ms）。两个参数任一为负→永不 purge，任一为 0→立即 purge，乘法溢出则退回 `delay` 本身。**这解释了一个常见困惑：为什么设了 `PURGE_DELAY=1000`，arena 里的内存 4 秒后才被归还。**

**⑥ verbose 分级与消息门控**（[src/options.c:516-530](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L516-L530)）：

```c
void _mi_trace_message(const char* fmt, ...) {
  if (mi_option_get(mi_option_verbose) <= 1) return;  // only with verbose level 2 or higher
  ...
}

void _mi_verbose_message(const char* fmt, ...) {
  if (!mi_option_is_enabled(mi_option_verbose)) return;
  ...
```

`MIMALLOC_VERBOSE=1` 打普通 verbose 消息；`MIMALLOC_VERBOSE=2`（数字解析路径）额外打开 trace 级消息。而错误/警告消息受另一层门控（[src/options.c:532-538](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L532-L538)）：verbose 开则全放行，否则要 `show_errors` 开且未超 `max_errors/max_warnings` 条数上限。

**⑦ 观测 purge 行为的窗口**：统计输出的 arenas 段有 `purged`（字节数）与 `purges`（次数）两行（[src/stats.c:404-416](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L404-L416)），只要进程用过 arena 就会打印（不依赖 debug 构建的细粒度统计）。

#### 4.4.4 代码实践

**实践目标**：用统计里的 `purges/purged` 行，直观看到 `purge_delay` 三个取值（-1 / 1000 / 0）的行为差异。

**操作步骤**（**示例代码**）：

```c
/* options-purge.c: 分配再释放一批内存，观察 purge 行为 */
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  for (int round = 0; round < 200; round++) {
    void* p = mi_malloc(64 * 1024);   /* 每轮 64KiB */
    mi_free(p);
  }
  return 0;   /* 退出时 SHOW_STATS 打印统计 */
}
```

```bash
gcc -I include options-purge.c out/release/libmimalloc.a -lpthread -lm -o options-purge

for d in -1 1000 0; do
  echo "=== PURGE_DELAY=$d ==="
  MIMALLOC_SHOW_STATS=1 MIMALLOC_PURGE_DELAY=$d ./options-purge 2>&1 | grep -E "purge|arenas"
done
```

**需要观察的现象**：

- `PURGE_DELAY=-1`：`purges`/`purged` 应为 0 或极小（总入口被 `<0` 短路）。
- `PURGE_DELAY=0`：立即 purge，`purges` 计数明显增多。
- 默认 `1000`：介于两者之间，arena 级还要乘 `arena_purge_mult=4`，短生命周期的程序可能来不及触发就退出了。

**预期结果**：三组统计呈上述梯度。本程序运行极快、purge 又依赖内部时机（页面 retire、延迟到期），具体计数**待本地验证**；若想放大差异，可把循环加大到数万轮或在末尾加 `sleep(6)` 让延迟到期后再退出。

#### 4.4.5 小练习与答案

**练习 1**：用户设了 `MIMALLOC_PURGE_DELAY=100`，为什么观察到的 arena purge 间隔可能是 400ms？
**答案**：arena 级延迟是 `purge_delay × arena_purge_mult`（[src/arena.c:2244-2251](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2244-L2251)），`arena_purge_mult` 默认 4，所以 100×4=400ms。想让 arena 也立即 purge，可以把 `arena_purge_mult` 设 0，或直接 `PURGE_DELAY=0`（此时乘积判 0 分支返回 0）。

**练习 2**：`MIMALLOC_VERBOSE=2` 比 `=1` 多看到什么？
**答案**：多出 trace 级消息。门控在 [src/options.c:516-522](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L516-L522)：`_mi_trace_message` 要求 verbose 值 ≥ 2，普通 `_mi_verbose_message` 只要求非 0。

**练习 3**：为什么 `mi_options_print` 里打印每个选项前要先 `mi_option_get` 一次？
**答案**：见 [src/options.c:235-240](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L235-L240)，循环里的 `mi_option_get(option); MI_UNUSED(l); // possibly initialize`——如果用户在 `main` 里第一次调用 `mi_options_print`（而某个选项从未被任何人读过、preload 阶段又没初始化成功），先 get 一遍保证打印出来的是解析环境后的最终值，而不是表里的原始默认值。

## 5. 综合实践

**任务**：写一个 `options-probe.c`，用**环境变量**和**编程接口**两条途径复现同一组配置，对比两者的输出差异，并解释差异根源。

**完整代码（示例代码）**：

```c
/* options-probe.c
 * 用途：对比「环境变量调参」与「程序内 mi_option_set 调参」的输出差异
 * 编译：gcc -I include options-probe.c out/release/libmimalloc.a -lpthread -lm -o options-probe
 */
#include <stdio.h>
#include <mimalloc.h>

int main(int argc, char** argv) {
  int mode = (argc > 1 ? atoi(argv[1]) : 0);   /* 0=只靠环境变量, 1=程序内调参 */

  if (mode == 1) {
    /* 编程接口途径：等价于 MIMALLOC_PURGE_DELAY=0 + MIMALLOC_VERBOSE=1 */
    mi_option_set(mi_option_purge_delay, 0);
    mi_option_enable(mi_option_verbose);
  }

  /* 制造一批分配/释放，让 verbose 消息和 purge 有机会发生 */
  for (int i = 0; i < 500; i++) {
    void* p = mi_malloc(8 * 1024);
    mi_free(p);
  }

  printf("purge_delay = %ld, verbose = %ld\n",
         mi_option_get(mi_option_purge_delay),
         mi_option_get(mi_option_verbose));
  return 0;
}
```

**运行三组对照**：

```bash
# 组 1：默认（什么都不设）
./options-probe 0

# 组 2：环境变量途径
MIMALLOC_PURGE_DELAY=0 MIMALLOC_VERBOSE=1 ./options-probe 0

# 组 3：编程接口途径（不设任何环境变量）
./options-probe 1
```

**需要观察并记录到一张对比表里的内容**：

| 观察点 | 组 1（默认） | 组 2（环境变量） | 组 3（编程接口） |
|---|---|---|---|
| 版本横幅 + 47 行选项全表打印 | 无 | **有**（在 `main` 前，`_mi_options_post_init` 打印） | **无**（enable 发生在 post_init 之后） |
| `mimalloc:` 前缀的 verbose 消息 | 无 | 有（含 process init 等早期消息） | 有（只有 `main` 之后的消息） |
| 末尾 `printf` 的两个值 | 1000 / 0 | 0 / 1 | 0 / 1 |

**预期结果**：组 2 与组 3 的**最终选项值完全一致**，但组 2 多出「main 之前的整表打印和早期 verbose 消息」——根源是 `_mi_options_post_init`（[src/options.c:206-209](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L206-L209)）在库加载时就检查 verbose 并打印，而组 3 的 enable 晚于这个时点。横幅具体内容与消息条数**待本地验证**。

**加分项**：给组 3 再补一组 `MIMALLOC_PURGE_DELAY=100 ./options-probe 1`，验证程序内 `mi_option_set` 是否覆盖了环境变量（末尾应打印 0），把结论与你对 `mi_option_set`（[src/options.c:302-316](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L302-L316)）的理解对照。

## 6. 本讲小结

- mimalloc 有 **47 个选项**（3 稳定 + 44 高级），定义是「三处联动」：`mimalloc.h` 的枚举定顺序与名字，`options.c` 的 `mi_options[]` 表存默认值与新旧名，`internal.h` 的 `mi_option_desc_t` 是五字段容器；**枚举顺序必须与表顺序一致**。
- 设置途径共**四种**，优先级从高到低：`mi_option_set`（含环境变量解析成功的写入）> 环境变量 > `mi_option_set_default`（只改未被显式设置过的选项）> 表默认值（可被编译期 `MI_DEFAULT_*` / `-DMI_EXTRA_CPPDEFS` 覆盖）。
- 环境变量名 = `mimalloc_` + 小写选项名，匹配**大小写不敏感**（Unix 下直接扫 `environ`，且能在 C 运行时初始化前使用）；值支持布尔词表（`1/TRUE/YES/ON`、`0/FALSE/NO/OFF`，**空串算 1**）与 `strtol` 数字，4 个 KiB 型选项额外支持 `K/M/G/T` 后缀。
- 每个选项有**三态** `init`：UNINIT（未查环境，可能因 preload 暂不可读而重试）→ DEFAULTED（环境没有，用默认）/ INITIALIZED（环境或程序设置）。`mi_option_get` 惰性触发解析；热路径用 `_mi_option_get_fast` 裸读，其安全性由 `_mi_options_init` 在进程加载时把全部选项初始化一遍来保证。
- `purge_delay` 是长时服务的核心调优项：`-1` 在 `_mi_os_purge_ex` 总入口短路（彻底不 purge），`0` 立即 purge，正值经 `mi_arena_purge_delay` 放大为 `purge_delay × arena_purge_mult`（默认 ×4）；行为可通过 `MIMALLOC_SHOW_STATS=1` 输出的 `purges/purged` 行观测。
- 环境变量 `MIMALLOC_VERBOSE=1` 会在 `main` 前由 `_mi_options_post_init` 打印**版本 + 全部选项表**，而 `main` 里 `mi_option_enable(mi_option_verbose)` 只影响之后的消息——这是两种调参方式最直观的输出差异。

## 7. 下一步学习建议

本讲之后，「会用 + 会调」这条入门线（单元一、单元二）就完整了。接下来：

1. **进入单元三（推荐下一步）**：`u3-l1 堆的层级模型`。选项里的 `purge_delay`、`arena_eager_commit` 都作用于「页」和「arena」这些对象，只有建立了 heap→theap→page→block 的层级心智地图，调优才真正有的放矢。
2. **想先看 purge 落到实处**：跳读 `u6-l2 os.c：commit/decommit/purge` 与 `u6-l3 arena：1GiB 内存区、64KiB slice`，理解 `_mi_os_purge_ex` 底层的 `MADV_DONTNEED/MADV_FREE` 差异。
3. **源码延伸阅读**：带着本讲的问题重读 [src/init.c:536-570](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L536-L570)，注意 `reserve_huge_os_pages` 等选项是如何在初始化序列中被 `mi_option_get_clamp` 消费的——这是「选项驱动初始化行为」的又一个实例。
