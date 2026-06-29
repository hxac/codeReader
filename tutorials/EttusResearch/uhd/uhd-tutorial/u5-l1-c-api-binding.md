# C API 绑定与错误处理

## 1. 本讲目标

UHD 本体是用 C++ 写的，抛的是 C++ 异常、用的是 `std::string`/`std::shared_ptr`。但很多语言的绑定（Python、Julia、MATLAB、Rust）以及一些只能链接 C ABI 的工程环境，没法直接吃下 C++ 异常和 STL 类型。为此 UHD 提供了一套**纯 C 的 API**，用「错误码 + 不透明句柄 + 出参指针」这套 C 语言里最常见的契约，把 `multi_usrp` 整个高层 API 暴露出来。

本讲学完后你应当能够：

1. 说清 C API 为什么存在，以及它遵循的「返回错误码、结果走出参」契约。
2. 读懂 `usrp.h` 里句柄类型、C 结构体和成百上千个 USRP 函数的组织方式。
3. 解释 `error.h` 的 `uhd_error` 错误码体系，以及 C++ 异常是如何被两个宏转换成错误码的。
4. 看懂 `usrp_c.cpp` 的「句柄注册表」设计：为什么 C 句柄不直接持有 C++ 对象指针，而是用一个整数索引查全局表。
5. 手写一个最小的 C 程序：发现设备 → 打印设备地址 → 释放，并正确读取错误信息。

## 2. 前置知识

在进入本讲前，你需要先建立以下概念（来自前面几讲）：

- **`multi_usrp`**（u2-l3）：UHD 最常用的高层设备门面类，用 `multi_usrp::make(device_addr)` 构造，提供收发频率/增益/采样率/天线/时间同步等一整套方法。C API 本质上就是它的一个 C 外壳。
- **`UHD_API` 宏**（u1-l4）：标记一个符号是否随 `libuhd` 导出。在 C API 里，每一个对外函数都带 `UHD_API`，否则链接器找不到它。
- **`device::find`**（u2-l1）：设备发现入口，返回 `device_addrs_t`（一组 `device_addr_t`）。C API 的 `uhd_usrp_find` 就是包了它。
- **C++ 异常**：UHD 内部用 `uhd::exception` 派生体系（`runtime_error`、`value_error`、`key_error`、`assertion_error` 等）报错。C 语言没有异常机制，这是 C API 要解决的核心矛盾。
- **`UHD_SINGLETON_FCN`**：UHD 自带的「函数内静态单例」宏，用来安全地获得一个进程级全局对象（这里用来存全局错误串和句柄表）。

一个关键直觉：**C 是「最小公倍数」**。几乎所有现代语言都能调用 C ABI，但几乎没有两种语言能直接互调 C++ ABI。所以 UHD 用一层薄薄的 C 包装，换来跨语言、跨编译器、跨 ABI 的可链接性。Python 绑定（u5-l2）在底层也正是链接这套 C API 或等价的 C++ 符号。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [host/include/uhd/usrp/usrp.h](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/usrp.h) | C API 的**公共声明**：句柄 typedef、C 结构体（`uhd_stream_args_t` 等）、USRP/RX/TX/主板/GPIO 全套函数原型。本讲的「接口表面」。 |
| [host/include/uhd/error.h](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/error.h) | 错误码枚举 `uhd_error`、异常转错误码的两个宏 `UHD_SAFE_C` / `UHD_SAFE_C_SAVE_ERROR`、`uhd_get_last_error` 声明。 |
| [host/lib/usrp/usrp_c.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp) | C API 的**实现**：句柄注册表、`make`/`free`/`find`、C↔C++ 结构体转换、每一个 USRP 方法的桥接。 |
| [host/lib/error_c.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/error_c.cpp) | 异常→错误码映射 `error_from_uhd_exception`、全局错误字符串的单例与互斥锁、`uhd_get_last_error` 实现。 |
| [host/include/uhd.h](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd.h) | C API 的**伞式头**（umbrella header）：一个 `#include <uhd.h>` 就拉进所有 C 子模块。 |
| [host/examples/rx_samples_c.c](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_c.c) | 官方 C 示例，展示了句柄生命周期与错误处理的惯用法。 |

> 提醒：C API 是一个**可选组件**。在 CMake 里由 `ENABLE_C_API` 开关控制，默认开启，但它依赖主组件 `ENABLE_LIBUHD`（见 [host/CMakeLists.txt:479](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt)）。如果关掉，`usrp.h`、`error.h` 都不会被安装，`libuhd` 里也不会有 `uhd_usrp_*` 符号。

---

## 4. 核心概念与源码讲解

### 4.1 usrp.h：C API 的类型系统与函数契约

#### 4.1.1 概念说明

`usrp.h` 是 C API 用户打交道最多的头文件。它要做的事情，本质上是用 C 语言的有限表达力，复刻 `multi_usrp` 这个 C++ 类的接口。这带来三条贯穿全局的设计约定：

1. **不透明句柄（opaque handle）**。C 这边永远拿不到 C++ 对象的内存布局，只拿到一个指针 `uhd_usrp_handle`。这个指针指向的 `struct uhd_usrp` 的内部字段在头文件里**完全不公开**——用户只能通过函数来操作它。这相当于 C 版的「封装」。

2. **返回错误码、结果走出参**。每一个 C API 函数的返回类型都是 `uhd_error`（一个枚举整数），真正的「返回值」通过指针型出参写回。比如读采样率不是 `double get_rx_rate(...)`，而是 `uhd_usrp_get_rx_rate(h, chan, &rate_out)`。

3. **C 结构体充当 DTO**。C++ 里用类的字段或 `stream_args_t` 这种对象表达的配置，在 C 这边被展平成纯结构体，比如 `uhd_stream_args_t`、`uhd_stream_cmd_t`。字段是裸 `char*`、`size_t*`、`bool`，不涉及任何 STL。

理解这三条，你就能在没有文档的情况下「猜」出任意一个 C API 函数该怎么用。

#### 4.1.2 核心流程

一次典型的 C API 调用，数据流是单向且对称的：

```
用户 C 代码
   │  1. 先 make 出一个句柄（拿到不透明指针）
   ▼
uhd_usrp_make(&h, "type=x300")
   │  2. 用句柄 + 出参指针调用业务函数
   ▼
uhd_usrp_get_rx_rate(h, 0, &rate)   ──►  内部: USRP(h)->get_rx_rate(0)
   │       └─ 结果写回 rate
   │  3. 检查返回的 uhd_error；非零时读 last_error
   ▼
if (err) uhd_usrp_last_error(h, buf, len);
   │  4. 用完务必 free 句柄
   ▼
uhd_usrp_free(&h)
```

要点：**句柄必须先 make 再用，用完必须 free**。头文件里反复出现的警告就是这条——在 make 之前用、或在 free 之后用，都会段错误。

#### 4.1.3 源码精读

**不透明句柄的定义**。注意 `struct uhd_usrp;` 只是前置声明，真正的字段藏在 `usrp_c.cpp` 里（4.3 节会看到）：

```c
struct uhd_usrp;
typedef struct uhd_usrp* uhd_usrp_handle;
```

这就是 [host/include/uhd/usrp/usrp.h:247-L255](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/usrp.h#L247-L255) 里「C-level interface for working with a USRP device」的写法。RX/TX 流器也是同样套路（[usrp.h:93-L106](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/usrp.h#L93-L106)）。

**C 结构体 DTO**。以流参数为例，它就是 `uhd::stream_args_t` 的 C 镜像，字段全是裸类型：

```c
typedef struct {
    char* cpu_format;      // 主机内存格式，如 "fc32"
    char* otw_format;      // 线上格式，如 "sc16"
    char* args;            // 其它键值参数
    size_t* channel_list;  // 通道数组
    int n_channels;        // 通道数
} uhd_stream_args_t;
```

见 [usrp.h:46-L58](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/usrp.h#L46-L58)。注意 `channel_list` 是指针 + 长度的经典 C 数组表达，对应 C++ 的 `std::vector<size_t>`。

**生命周期四件套**。整个 USRP 接口的「脊柱」是这四个函数，几乎每个 C 程序都会用到：

```c
UHD_API uhd_error uhd_usrp_find(const char* args, uhd_string_vector_handle* strings_out);
UHD_API uhd_error uhd_usrp_make(uhd_usrp_handle* h, const char* args);
UHD_API uhd_error uhd_usrp_free(uhd_usrp_handle* h);
UHD_API uhd_error uhd_usrp_last_error(uhd_usrp_handle h, char* error_out, size_t strbuffer_len);
```

见 [usrp.h:264-L286](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/usrp.h#L264-L286)。注意几个细节：

- `find` **不接受句柄**，它返回的是一组「设备地址字符串」到一个 `string_vector` 出参里——因为你还没 `make` 任何设备。
- `make` 的入参是「指向句柄的指针」`uhd_usrp_handle*`，因为它要给你写回一个新句柄。
- `last_error` 用 `char*` + `strbuffer_len` 这种 C 式「调用方分配缓冲、被调方填充」的方式返回字符串——这是 C API 返回字符串的统一模式，到处都是。

**出参模式的具体例子**。读采样率：

```c
UHD_API uhd_error uhd_usrp_get_rx_rate(uhd_usrp_handle h, size_t chan, double* rate_out);
```

见 [usrp.h:515-L517](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/usrp.h#L515-L517)。返回值只表达「成功/失败」，真正的采样率写进 `rate_out` 指向的 `double`。这和 C++ 的 `double get_rx_rate(chan)` 形成了鲜明对比。

**伞式头**。`usrp.h` 本身已经 include 了一堆 C 类型头（`metadata.h`、`ranges.h`、`tune_request.h` 等，见 [usrp.h:10-L23](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/usrp.h#L10-L23)）；而更高一层的 [uhd.h](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd.h) 把所有 C 模块一次性拉齐。C 程序通常只需 `#include <uhd.h>`。

#### 4.1.4 代码实践

**实践目标**：用眼睛把 `usrp.h` 的「契约」内化为肌肉记忆。

**操作步骤**：

1. 打开 [usrp.h](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/usrp.h)。
2. 在 RX methods 段（约 [usrp.h:490-L677](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/usrp.h#L490-L677)）里，随机挑 3 个 `get_*` 函数，确认它们都长成 `(handle, 入参..., 出参指针)` 的样子。
3. 找到返回字符串的函数（如 `uhd_usrp_get_mboard_name`），确认它的字符串出参一定是 `(char* out, size_t len)` 成对出现。

**需要观察的现象**：所有函数签名高度同构——没有任何一个函数通过返回值传业务数据，返回值永远是 `uhd_error`。

**预期结果**：你会得到一个判断口诀——「看到 `uhd_usrp_*` 函数，先找它最后那个 `_out` 指针，那才是真正结果」。

> 待本地验证：若你想统计函数总数，可在仓库里对 `usrp.h` 跑 `grep -c '^UHD_API uhd_error uhd_'`，得到 C API 暴露的函数个数。

#### 4.1.5 小练习与答案

**练习 1**：`uhd_usrp_find` 为什么不接收 `uhd_usrp_handle`，而 `uhd_usrp_get_rx_rate` 必须接收？

**参考答案**：`find` 的目的是「在还没有设备对象时，列出有哪些设备」，所以它没有句柄可收，结果通过一个独立的 `string_vector` 出参返回；而 `get_rx_rate` 操作的是「已经 make 出来的某台设备」，必须知道操作哪一台，所以要传句柄。

**练习 2**：下列哪个字段不在 `uhd_stream_args_t` 里？(a) `cpu_format` (b) `channel_list` (c) `sample_rate` (d) `n_channels`。

**参考答案**：(c) `sample_rate`。采样率不是流参数，而是通过 `uhd_usrp_set_rx_rate` 单独设置；流参数只描述「数据格式 + 通道映射 + 杂项 args」。

---

### 4.2 error.h：错误码体系与异常到错误码的转换

#### 4.2.1 概念说明

C 这边没有 `try/catch`，UHD 内部却到处抛 C++ 异常。C API 的核心难题就是：**怎么把一个跨越 C++/C 边界的异常，安全地翻译成 C 能理解的东西？** `error.h` 给出了答案——把它压成一个整数错误码 `uhd_error`，外加一个可查询的错误字符串。

这套机制有两层：

- **错误码**：一个枚举，每一个值对应一类 `uhd::exception` 派生类（如 `UHD_ERROR_VALUE` ↔ `uhd::value_error`），另外还有给 `boost::exception`、`std::exception` 和「完全不认识」兜底的码。
- **错误字符串**：异常的 `what()` 文本被存起来，用户随后可以取回。存的地方有两个——一个进程级全局缓冲（给不接收句柄的函数用），一个句柄内缓冲（给接收句柄的函数用）。这是为了「无句柄的函数也能查错误」。

把异常翻译成错误码这件事，是用两个宏 `UHD_SAFE_C` 和 `UHD_SAFE_C_SAVE_ERROR` 统一完成的——这也是为什么 `usrp_c.cpp` 里几乎每个函数体都裹在这两个宏之一里。

#### 4.2.2 核心流程

异常→错误码的转换流程如下：

```
   C API 函数体（C++ 代码，可能抛异常）
            │
            ▼  抛出 uhd::value_error
   ┌─────────────────────────────────────┐
   │  UHD_SAFE_C_SAVE_ERROR(h, ...) 宏    │
   │                                       │
   │  try { ...业务代码... }               │
   │  catch (uhd::exception& e) {          │
   │      1) set_c_global_error_string(e.what())  ← 写全局缓冲
   │      2) h->last_error = e.what()             ← 写句柄缓冲
   │      3) return error_from_uhd_exception(&e)  ← 翻译成 UHD_ERROR_VALUE
   │  }                                    │
   │  catch (boost::exception& e) { ... UHD_ERROR_BOOSTEXCEPT } │
   │  catch (std::exception&  e) { ... UHD_ERROR_STDEXCEPT  }   │
   │  catch (...)                { ... UHD_ERROR_UNKNOWN    }   │
   │  （正常路径）set_c_global_error_string("None"); return UHD_ERROR_NONE │
   └─────────────────────────────────────┘
            │
            ▼  返回整数错误码给 C 调用方
   用户读取：uhd_usrp_last_error(h, ...) 或 uhd_get_last_error(...)
```

两个宏的差别只有一点：`UHD_SAFE_C_SAVE_ERROR(h, ...)` 额外把错误写进 `h->last_error`，因此**只在有句柄的函数里能用**；`UHD_SAFE_C(...)` 只写全局缓冲，用在 `make`/`find` 这类还没有（或不接收）句柄的地方。

#### 4.2.3 源码精读

**错误码枚举**。每一个码都注释了它对应的 C++ 异常类型：

```c
typedef enum {
    UHD_ERROR_NONE = 0,           // 无错误
    UHD_ERROR_INVALID_DEVICE = 1, // 设备参数非法
    UHD_ERROR_INDEX = 10,         // ↔ uhd::index_error
    UHD_ERROR_KEY = 11,           // ↔ uhd::key_error
    /* ... */
    UHD_ERROR_VALUE = 43,         // ↔ uhd::value_error
    UHD_ERROR_RUNTIME = 44,       // ↔ uhd::runtime_error
    /* ... */
    UHD_ERROR_BOOSTEXCEPT = 60,   // ↔ boost::exception
    UHD_ERROR_STDEXCEPT = 70,     // ↔ std::exception
    UHD_ERROR_UNKNOWN = 100       // 完全不认识的异常
} uhd_error;
```

完整定义见 [error.h:20-L67](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/error.h#L20-L67)。

**异常→错误码的映射函数**。它住在 `error_c.cpp`，用一连串 `dynamic_cast` 把异常精确归类：

```cpp
#define MAP_TO_ERROR(exception_type, error_type) \
    if (dynamic_cast<const uhd::exception_type*>(e)) return error_type;

uhd_error error_from_uhd_exception(const uhd::exception* e)
{
    MAP_TO_ERROR(index_error,         UHD_ERROR_INDEX)
    MAP_TO_ERROR(key_error,           UHD_ERROR_KEY)
    MAP_TO_ERROR(value_error,         UHD_ERROR_VALUE)
    MAP_TO_ERROR(runtime_error,       UHD_ERROR_RUNTIME)
    /* ... 其余映射 ... */
    return UHD_ERROR_EXCEPT;   // 兜底：是 uhd::exception 但没匹配上
}
```

见 [error_c.cpp:14-L35](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/error_c.cpp#L14-L35)。注意 `dynamic_cast` 的顺序：派生类必须排在基类前面，否则会被基类提前「截胡」。这里是按具体异常→通用异常的顺序排列的。

**两个核心宏**。`UHD_SAFE_C_SAVE_ERROR` 是最完整的版本（[error.h:113-L136](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/error.h#L113-L136)），它的每个 catch 分支都同时干三件事：写全局串、写 `h->last_error`、返回对应错误码。`UHD_SAFE_C`（[error.h:89-L106](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/error.h#L89-L106)）是它的简化版，没有 `h->last_error` 那一行。两个宏在正常返回前都把全局串设成 `"None"`，并在 `#ifdef __cplusplus` 内才生效——因为它们用了 C++ 的 try/catch。

**两处错误字符串**。`error.h` 的注释说得非常直白：

> Functions that do not take in UHD structs/handles will place any error strings into a buffer that can be queried with this function. Functions that do take in UHD structs/handles will place their error strings in **both** locations.

见 [error.h:141-L147](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/error.h#L141-L147)。这意味着：

- 调了 `uhd_usrp_find`（无句柄）失败 → 只能 `uhd_get_last_error(buf, len)` 取错误。
- 调了 `uhd_usrp_set_rx_rate`（有句柄）失败 → 既可 `uhd_usrp_last_error(h, buf, len)`，也可 `uhd_get_last_error(buf, len)`。

`uhd_get_last_error` 的实现在 [error_c.cpp:57-L67](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/error_c.cpp#L57-L67)，它从全局单例读串、`memset` 清缓冲、`strncpy` 拷贝——同样是「调用方给缓冲」的 C 模式。全局单例本身用 `UHD_SINGLETON_FCN` + 一把 `std::mutex` 保护（[error_c.cpp:41-L55](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/error_c.cpp#L41-L55)），保证多线程下读写安全。

#### 4.2.4 代码实践

**实践目标**：搞清「什么时候用 `last_error(handle)`，什么时候只能用 `uhd_get_last_error`」。

**操作步骤**：

1. 打开 [usrp_c.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp)。
2. 找到 `uhd_usrp_find`（[usrp_c.cpp:203-L213](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L203-L213)）和 `uhd_usrp_make`（[usrp_c.cpp:216-L234](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L216-L234)），确认它们用的是 `UHD_SAFE_C`（无 SAVE_ERROR）。
3. 再看 `uhd_usrp_set_rx_rate`（[usrp_c.cpp:590-L592](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L590-L592)），确认它用的是 `UHD_SAFE_C_SAVE_ERROR(h, ...)`。

**需要观察的现象**：用 `UHD_SAFE_C` 的函数恰好都是「没有有效句柄可写」的函数（find 根本不收句柄；make 此刻正在创建句柄、还没成型）；其余操作类函数全用 SAVE_ERROR 版。

**预期结果**：你应当能总结出规律——「错误码到处都能拿到（返回值），错误字符串的精确位置取决于这个函数有没有句柄」。

> 待本地验证：可选地在 `uhd_usrp_make` 里临时 `throw uhd::value_error("demo")`（仅本地实验，勿提交），重新编译后用 C 程序调 `uhd_usrp_make`，观察返回 `UHD_ERROR_VALUE`，且 `uhd_get_last_error` 能取回 `"demo"`。

#### 4.2.5 小练习与答案

**练习 1**：一个 C 程序调 `uhd_usrp_find` 失败，返回 `UHD_ERROR_INVALID_DEVICE`。它该用哪个函数拿到错误描述？

**参考答案**：用 `uhd_get_last_error(buf, len)`。因为 `find` 不接收句柄，错误只写进了全局缓冲，没有句柄级 `last_error` 可查。

**练习 2**：为什么 `error_from_uhd_exception` 用 `dynamic_cast` 而不是 C++ 的 `catch` 多分支来归类？

**参考答案**：因为这里拿到的是一个 `const uhd::exception*` 指针（已经被宏 catch 住、传过来的），面对的是「一个指针指向哪个派生类」的问题，`dynamic_cast` 正是干这个的；而 `catch` 分支已经在宏里用过了，宏把每种基类 catch 住后才调用本函数做**更细**的子类归类。

---

### 4.3 usrp_c.cpp：句柄注册表、生命周期与 C↔C++ 桥接

#### 4.3.1 概念说明

`usrp_c.cpp` 是把 C 声明变成 C++ 行为的地方。它要回答一个看似简单实则巧妙的设计问题：**C 句柄 `uhd_usrp_handle` 内部到底该存什么？**

一种朴素的方案是直接把 `multi_usrp::sptr`（C++ 共享指针）塞进句柄结构体。但这会暴露 C++ 类型给 C 头文件，破坏 ABI 隔离。UHD 的实际选择是：**句柄只存一个整数索引 `usrp_index`，真正的 C++ 对象放在一个进程级的全局 `std::map` 里**。这个 map 就是「句柄注册表」。

这个设计有三个好处：

1. **ABI 隔离**：C 头文件完全不需要知道 `shared_ptr`、`multi_usrp` 是什么。
2. **稳定**：索引是个单调递增计数器，释放后不回收，所以「悬空句柄」不会意外指向新对象——查表会 miss。
3. **集中管理**：所有 C++ 对象的生命周期在一个地方，方便加锁和调试。

代价是每次调用都要多一次 map 查找——但相比真正的硬件 IO，这可以忽略。

此外，几乎每个 C API 函数都配了一把自己的 `static std::mutex`，把对应操作**串行化**。这是 C API 一个重要特性：**它默认不是高并发的**，跨线程高频调用要注意这层锁。

#### 4.3.2 核心流程

**创建（make）**：

```
uhd_usrp_make(&h, args)
   │  取一个新 usrp_index = usrp_counter++
   │  multi_usrp::make(device_addr(args))  → sptr
   │  注册表[index] = sptr
   │  h = new uhd_usrp{index, ""}
   ▼  返回 UHD_ERROR_NONE
```

**调用（任意方法）**：

```
uhd_usrp_set_rx_rate(h, rate, chan)
   │  宏 USRP(h) 展开为 注册表[h->usrp_index].ptr
   │  → 调 multi_usrp::set_rx_rate(rate, chan)
   ▼  异常被 UHD_SAFE_C_SAVE_ERROR 翻译成错误码
```

**释放（free）**：

```
uhd_usrp_free(&h)
   │  注册表.erase(h->usrp_index)   ← sptr 引用计数 -1，可能析构设备
   │  delete h; *h = NULL
   ▼
```

**发现（find）**：不碰注册表，直接调 `device::find(args, USRP)`，把每个 `device_addr_t` 转成字符串塞进出参 `string_vector`。

#### 4.3.3 源码精读

**句柄与注册表的真实结构**。这就是头文件里「不公开」的内部：

```cpp
struct uhd_usrp {                 // 句柄本体：只有索引 + 错误串
    size_t usrp_index;
    std::string last_error;
};

struct usrp_ptr {                 // 注册表里的值：真正的 C++ 对象
    uhd::usrp::multi_usrp::sptr ptr;
    static size_t usrp_counter;   // 单调递增的索引计数器
};

typedef std::map<size_t, usrp_ptr> usrp_ptrs;
UHD_SINGLETON_FCN(usrp_ptrs, get_usrp_ptrs);   // 进程级单例 map

// 句柄 → C++ 对象 的快捷宏
#define USRP(h_ptr) (get_usrp_ptrs()[h_ptr->usrp_index].ptr)
```

见 [usrp_c.cpp:51-L84](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L51-L84)。注释里那句 `/* Prefer map, because the list can be discontiguous */` 解释了为什么用 `map` 而不是 `vector`：释放后索引会留下空洞（discontiguous），map 天然支持稀疏键。

**make 的实现**。注意它怎么分配索引、造对象、入表、回填句柄：

```cpp
uhd_error uhd_usrp_make(uhd_usrp_handle* h, const char* args) {
    UHD_SAFE_C(std::lock_guard<std::mutex> lock(_usrp_make_mutex);
        size_t usrp_count = usrp_ptr::usrp_counter;
        usrp_ptr::usrp_counter++;                       // ① 领号
        uhd::device_addr_t device_addr(args);
        usrp_ptr P;
        P.ptr = uhd::usrp::multi_usrp::make(device_addr); // ② 真正造设备
        get_usrp_ptrs()[usrp_count] = P;                // ③ 入注册表
        (*h) = new uhd_usrp;                            // ④ 造句柄
        (*h)->usrp_index = usrp_count;)
}
```

见 [usrp_c.cpp:216-L234](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L216-L234)。第 ② 步正是 u2-l3 讲过的 `multi_usrp::make`，C API 只是在它外面包了号牌管理。

**free 的实现**。注意它先检查索引是否存在（防悬空），再擦除、删句柄：

```cpp
uhd_error uhd_usrp_free(uhd_usrp_handle* h) {
    UHD_SAFE_C(std::lock_guard<std::mutex> lock(_usrp_free_mutex);
        if (!get_usrp_ptrs().count((*h)->usrp_index)) {
            return UHD_ERROR_INVALID_DEVICE;            // 句柄已失效
        }
        get_usrp_ptrs().erase((*h)->usrp_index);        // sptr 引用 -1
        delete *h;
        *h = NULL;)
}
```

见 [usrp_c.cpp:237-L248](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L237-L248)。把句柄指针置 NULL，是为了让调用方「再次 free」可被检测。

**find 的实现**。它直接借力 `device::find`，把每个地址转字符串：

```cpp
uhd_error uhd_usrp_find(const char* args, uhd_string_vector_handle* strings_out) {
    UHD_SAFE_C(std::lock_guard<std::mutex> _lock(_usrp_find_mutex);
        uhd::device_addrs_t devs = uhd::device::find(std::string(args), uhd::device::USRP);
        (*strings_out)->string_vector_cpp.clear();
        for (const uhd::device_addr_t& dev : devs) {
            (*strings_out)->string_vector_cpp.push_back(dev.to_string());
        })
}
```

见 [usrp_c.cpp:203-L213](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L203-L213)。`uhd::device::USRP` 是设备过滤类型（u2-l2 讲过的 `device_filter_t`），限定只找 USRP 类设备。

**C↔C++ 结构体桥接**。这是 C API 的另一项体力活——把 C 结构体翻译成 C++ 对象。以流参数为例：

```cpp
uhd::stream_args_t stream_args_c_to_cpp(const uhd_stream_args_t* c) {
    uhd::stream_args_t cpp(c->cpu_format, c->otw_format);
    cpp.args     = c->args;
    cpp.channels = std::vector<size_t>(c->channel_list, c->channel_list + c->n_channels);
    return cpp;
}
```

见 [usrp_c.cpp:21-L34](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L21-L34)。指针+长度的 C 数组被还原成 `std::vector`。`get_rx_stream` 就靠它把 C 端的 `uhd_stream_args_t` 喂给 `multi_usrp::get_rx_stream`（见 [usrp_c.cpp:256-L271](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L256-L271)）。

**普通方法的极简实现**。绝大多数方法都是「一行宏 + 一行 C++ 调用」，例如设采样率：

```cpp
uhd_error uhd_usrp_set_rx_rate(uhd_usrp_handle h, double rate, size_t chan) {
    UHD_SAFE_C_SAVE_ERROR(h, USRP(h)->set_rx_rate(rate, chan);)
}
```

见 [usrp_c.cpp:590-L592](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L590-L592)。`USRP(h)` 取回 `multi_usrp::sptr`，直接调同名方法，异常由宏翻译。整个文件几百行，几乎都是这个模板的复刻——这也是它能用很少代码覆盖整个 `multi_usrp` 接口的原因。

#### 4.3.4 代码实践

**实践目标**：跟踪一条完整的 C→C++ 调用链，把三个模块（句柄、错误、桥接）串起来。

**操作步骤**：

1. 从 C 声明 [usrp.h:513-L514](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/usrp.h#L513-L514) 的 `uhd_usrp_set_rx_rate` 出发。
2. 跳到实现 [usrp_c.cpp:590-L592](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L590-L592)。
3. 展开 `USRP(h)` → `get_usrp_ptrs()[h->usrp_index].ptr`（[usrp_c.cpp:84](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp_c.cpp#L84)）。
4. 最终落到 `multi_usrp::set_rx_rate(rate, chan)`（u2-l3）。
5. 假设这次调用硬件拒绝了非法采样率，抛了 `uhd::value_error`，顺着 `UHD_SAFE_C_SAVE_ERROR`（[error.h:113-L136](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/error.h#L113-L136)）走一遍：catch → 写全局串 + `h->last_error` → `error_from_uhd_exception` → 返回 `UHD_ERROR_VALUE`。

**需要观察的现象**：一条「设采样率」的调用，沿途经过了 (a) C 声明 (b) 注册表查找 (c) C++ 方法 (d) 异常翻译 (e) 双重错误存储，五个环节。

**预期结果**：你能在一张图上画出这条链，并指出每个环节分别属于本讲的哪个模块。

> 待本地验证：可对照官方示例 [rx_samples_c.c:148-L153](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_c.c#L148-L153)，那里正是「设采样率 → 再回读实际采样率」的真实用法。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `uhd_usrp` 句柄里只存 `usrp_index`，而不直接存 `multi_usrp::sptr`？

**参考答案**：为了 ABI 隔离与稳定性。直接存 `sptr` 会让 C 头文件必须包含 C++ 的 `<memory>` 和 `multi_usrp.hpp` 的类型定义，破坏 C 接口的语言中立性；用整数索引查全局表，C 头只需前置声明 `struct uhd_usrp;`，且索引单调不回收，避免悬空句柄误命中新对象。

**练习 2**：`uhd_usrp_free` 里 `get_usrp_ptrs().erase(index)` 之后，`multi_usrp` 对象一定会立刻析构吗？

**参考答案**：不一定。`multi_usrp::sptr` 是共享指针，注册表里那份被擦除只让引用计数减一；如果还有别的 `sptr` 指向同一对象（例如某个 streamer 仍持有设备引用），对象要等最后一个引用消失才析构。当引用计数归零，设备才真正关闭。

**练习 3**：每个 C API 函数都自带一把 `static std::mutex`，这暗示了 C API 的什么使用约束？

**参考答案**：它把同类操作串行化了，说明 C API 不是为「多线程高频并发调用同一组函数」设计的。在多线程架构里，最好由用户自己用一个线程串行驱动 USRP，而不是从多个线程同时打 C API。

---

## 5. 综合实践

把本讲三个模块揉在一起，完成规格里要求的最小任务：**用 C API 写一段程序——`uhd_usrp_find` → 打印设备地址 → 释放，并说明错误码如何被读取。**

### 5.1 实践目标

- 亲手用一次 C API 的「出参 + 错误码」契约。
- 体验「无句柄函数（find）的错误只能从全局缓冲读」这一关键区别。
- 掌握 `string_vector` 这个发现结果容器的 make → 遍历 → free 生命周期。

### 5.2 示例代码

下面这段是**示例代码**（非项目原有文件），基于 [string_vector.h](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/string_vector.h) 的真实 API 和官方示例 [rx_samples_c.c](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_c.c) 的错误处理惯用法编写：

```c
/* 示例代码：发现并打印 USRP 设备地址 */
#include <uhd.h>
#include <stdio.h>
#include <stdlib.h>

int main(void)
{
    char err_buf[512] = {0};
    uhd_string_vector_handle devs = NULL;   /* 发现结果容器 */
    size_t num_devs = 0;

    /* ① make：先造一个空 string_vector 句柄接收结果 */
    if (uhd_string_vector_make(&devs) != UHD_ERROR_NONE) {
        uhd_get_last_error(err_buf, sizeof(err_buf));
        fprintf(stderr, "string_vector_make 失败: %s\n", err_buf);
        return EXIT_FAILURE;
    }

    /* ② find：发现 USRP 设备。args="" 表示不过滤；结果写入 devs。
       注意 find 不接收 handle，失败时错误只能用 uhd_get_last_error 读。 */
    uhd_error err = uhd_usrp_find("", &devs);
    if (err != UHD_ERROR_NONE) {
        uhd_get_last_error(err_buf, sizeof(err_buf));
        fprintf(stderr, "uhd_usrp_find 失败 (err=%d): %s\n", err, err_buf);
        uhd_string_vector_free(&devs);
        return EXIT_FAILURE;
    }

    /* ③ 遍历：先取大小，再逐个 at() 取字符串到自己的缓冲 */
    uhd_string_vector_size(devs, &num_devs);
    printf("发现 %zu 个设备:\n", num_devs);
    for (size_t i = 0; i < num_devs; i++) {
        char addr[512] = {0};
        uhd_string_vector_at(devs, i, addr, sizeof(addr));
        printf("  [%zu] %s\n", i, addr);
    }

    /* ④ free：释放容器句柄 */
    uhd_string_vector_free(&devs);
    return EXIT_SUCCESS;
}
```

### 5.3 操作步骤

1. 把上面的代码存为 `find_usrp.c`。
2. 确认你的 UHD 构建启用了 C API：在构建目录跑 `uhd_config_info --enabled-components`，应能看到 `LibUHD - C API`。
3. 编译并链接 `libuhd`：

   ```bash
   gcc find_usrp.c -o find_usrp -luhd
   ```

   （`-luhd` 即 u1-l3 讲过的 CMake target `uhd` 对应的库名。）
4. 运行 `./find_usrp`。

### 5.4 需要观察的现象与预期结果

- **有硬件时**：会打印类似 `发现 1 个设备: [0] type=x300,addr=192.168.10.2,...` 的地址串，正常退出。
- **无硬件时**：通常会打印 `发现 0 个设备:` 并以成功码退出（找不到设备并不抛异常）。这一点需要**待本地验证**，因为是否把「找不到」当错误取决于设备类型与 `find` 的内部行为。
- **故意制造错误**：把 `uhd_usrp_find("", &devs)` 改成传一个非法参数构造的场景（或断开网络 mid-call），观察返回的非零 `err` 以及 `uhd_get_last_error` 取回的描述串——这就是「错误码如何被读取」的完整演示。

### 5.5 错误码读取要点小结

| 函数类型 | 失败后取错误的方式 |
| --- | --- |
| 不接收句柄（`uhd_usrp_find`、各 `*_make`） | 只能 `uhd_get_last_error(buf, len)` |
| 接收句柄（`uhd_usrp_set_rx_rate` 等） | `uhd_usrp_last_error(h, buf, len)` **或** `uhd_get_last_error(buf, len)` 均可 |
| 任何函数 | 返回值 `uhd_error` 永远可直接判断成败 |

## 6. 本讲小结

- UHD 的 C API 用「**错误码 + 不透明句柄 + 出参指针**」三件套契约，把 C++ 的 `multi_usrp` 暴露给所有能链接 C ABI 的语言和环境。
- `usrp.h` 是接口表面：句柄是前置声明的 `struct uhd_usrp*`，配置用 `uhd_stream_args_t` 这类纯 C 结构体，每个函数都返回 `uhd_error`、业务结果走出参。
- `error.h` 用 `uhd_error` 枚举复刻 `uhd::exception` 体系，靠 `UHD_SAFE_C` / `UHD_SAFE_C_SAVE_ERROR` 两个宏把 C++ 异常统一翻译成错误码，并把 `what()` 存进「全局 + 句柄」两处缓冲。
- `error_c.cpp` 的 `error_from_uhd_exception` 用一串 `dynamic_cast` 把异常精确归类；全局错误串用单例 + 互斥锁保证线程安全。
- `usrp_c.cpp` 采用「**句柄注册表**」设计：句柄只存整数索引，真正的 `multi_usrp::sptr` 放在进程级 `std::map` 里，用 `USRP(h)` 宏查表，兼顾 ABI 隔离与悬空句柄安全。
- C API 每个函数自带一把互斥锁，把同类操作串行化，暗示它面向「单线程串行驱动」而非高并发。

## 7. 下一步学习建议

- **学 Python 绑定（u5-l2）**：对比 C API 和 pybind11 绑定两套「把 C++ 暴露出去」的路线，你会更深刻地理解 ABI 隔离的意义，并看到 `pyuhd` 如何复用同样的 C++ 对象。
- **回到 u2-l3/u2-l5 深读 `multi_usrp` 与 `stream`**：本讲每个 C 函数背后都是一个 C++ 方法，把两侧对照看，能同时巩固两套 API。
- **阅读官方 C 示例 `rx_samples_c.c` 全文**：它是本讲契约的完整应用——尤其它的 `EXECUTE_OR_GOTO` 宏（[rx_samples_c.c:17-L21](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_c.c#L17-L21)）给出了「错误即跳转清理」的 C 资源管理范式，值得模仿。
- **看 C API 测试**：`host/tests` 下 `ENABLE_C_API` 段（[host/tests/CMakeLists.txt:130](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/CMakeLists.txt)）里的用例展示了 C API 的预期行为，可作为权威断言来源。
