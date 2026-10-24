# C ABI 契约：pegainfer_cupti_profile_range 的 12 个参数

> 进阶层第 1 讲 · 前置：u1-l1「四个文件搭起的 CUPTI 桥」
>
> 先纠正一个容易踩的坑：教学规划稿的标题曾把这个函数写成「13 个参数」，但对真实签名逐个数，是 **12 个**。最常见的多数原因：把返回值 `int` 也当成参数，或把 `prepare_fn`/`launch_fn` 共享的 `userdata` 数了两次。本讲一切以真实签名为准（见 4.1.3 的链接）。

## 1. 本讲目标

学完本讲，你应该能够：

- 不看源码，按五组复述导出函数的 12 个参数：**输入上下文**（2 个）、**输入配置**（3 个）、**回调三元组**（3 个）、**输出缓冲**（2 个）、**错误通道**（2 个），并说出每组「谁准备、谁读写」。
- 解释**调用方预分配缓冲**契约：输出数组与错误缓冲都由调用方先备好再传入，C++ 侧只往里写，并用 `write_error` 做「装不下就截断」的收尾。
- 手算 `write_error` 的截断结果：给定 `error_len` 和任意长度的错误消息，写出缓冲里最终的字节。
- 按顺序说出入口处 4 条防御性校验（null context、null range 名、空指标列表、null 指针缓冲）各自对应的错误消息，并解释为什么这 4 条让「参数错误」在没有 GPU 的机器上也能复现。
- 把 C++ 签名里的每个类型一一对应到 Rust 侧 `unsafe extern` 声明的类型，理解 `Option<fn>` 的空指针优化在这条契约里扮演的角色。

u1-l1 已建立「四个文件、唯一入口、一次调用返回 `Vec<f64>`」的全景。本讲把放大镜对准 Rust 与 C++ 之间那条最窄的缝：**一个 extern "C" 函数的 12 个参数**。

## 2. 前置知识

本讲不需要 CUDA/CUPTI 背景知识，但需要以下「跨语言通话」的基础概念：

- **ABI（Application Binary Interface，应用二进制接口）**：两段已编译代码在二进制层面如何对话的规矩——参数放哪个寄存器/栈槽、返回值放哪、符号名长什么样。语言层面签名相同，不等于 ABI 兼容。
- **C ABI 是跨语言的「普通话」**：Rust 与 C++ 各有复杂的运行时与编译约定，双方都原生支持的公共子集是 C 调用约定。所以 Rust↔C++ 的桥几乎总是「两边各自声明同一个 extern "C" 函数」。
- **name mangling（名字修饰）**：C++ 编译器把参数类型编码进符号名（重载需要它），外部无法预测。`extern "C"` 关闭修饰，符号名就是函数名本身——Rust 同名声明才能链接到它。
- **「数组 + 长度」惯用法**：C 数组传参退化为裸指针，不带长度；所以凡传数组必配一个 `size_t` 计数（本例：`metric_names`+`metric_count`，`metric_values`+`metric_value_count`）。
- **回调函数指针 + userdata**：调用方把「以后要执行的事」包成函数指针传给被调方，被调方在合适时机反过来调用；`userdata` 是一个 `void*` 上下文指针，调用方塞进去什么、回调里就拿回什么。本讲只关注它在签名里的位置，蹦床机制在 u3-l1 深挖。
- **NUL 结尾字符串（C 字符串）**：以 `'\0'` 字节标记结尾的字节数组；Rust 侧对应 `CString`（构造）/`CStr`（借用读取）。
- **调用方分配缓冲（caller-allocated buffer）**：被调方不 malloc、不返回堆指针，而是调用方先分配好内存再把指针传入。好处：内存所有权始终在一方手里，没有「谁释放」的歧义。
- **异常绝不跨过 FFI**：C++ 异常靠栈展开（unwind），按 C 约定进入的调用者没有展开表，异常穿透 extern "C" 边界就是未定义行为。铁律：边界内的异常必须就地捕获，压缩成错误码/错误字符串再返回。

术语衔接：u1-l1 引入的 Range Profiler（区间性能剖析器）、回放（replay，CUPTI 为凑齐计数器多遍重跑 kernel）、蹦床回调（trampoline），本讲把它们当作「边界对面发生的事」，内部机制分别属于 u2-l2/u2-l3/u3-l1。

## 3. 本讲源码地图

本 crate 只有四个文件（u1-l1 已全景扫过），本讲引用集中在其中两个：

| 文件 | 总行数 | 本讲关注 | 留给后续讲义 |
|---|---|---|---|
| [csrc/range_profiler.cpp](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp) | 402 | L18 回调别名；L25-L32 `write_error`；L358-L402 导出函数 | L34-L77 错误字符串构造（u3-l2）；L78-L355 会话装配/回放/解码（u2-l2～u2-l4） |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs) | 145 | L7-L8 类型别名；L10-L25 `unsafe extern` 声明；L79-L92 包装签名；L100-L101 缓冲；L113-L128 调用点 | L48-L77 蹦床（u3-l1）；L134-L144 错误合并三分支（u2-l5） |
| [build.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs) | 34 | L20 产出静态库 `pegainfer_cupti_range_profiler`；L27-L31 链接 cuda/cupti/stdc++（第 5 节实践要用） | 其余（u1-l2） |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/Cargo.toml) | 12 | L2 `edition = "2024"`（`unsafe extern` 的版本背景）；无运行时依赖 | 构建依赖（u1-l2） |

## 4. 核心概念与源码讲解

### 4.1 extern "C" 导出函数完整签名

#### 4.1.1 概念说明

u1-l1 说 145 行的 lib.rs「收敛」了 402 行的 C++；收敛的锚点就是这一个函数。Rust 和 C++ 是两套编译体系，谁也不能直接调用对方的对象和异常，唯一共通的是 C 调用约定——于是这个 crate 的全部跨语言流量被压缩成一个 `extern "C"` 函数。

「签名即契约」：12 个参数不是随意排列的，按语义分成 5 组——

| 分组 | 参数 | 个数 | 谁准备 | 谁读写 |
|---|---|---|---|---|
| 输入上下文 | `context`、`device_index` | 2 | 调用方 | C++ 只读 |
| 输入配置 | `range_name`、`metric_names`、`metric_count` | 3 | 调用方 | C++ 只读 |
| 回调三元组 | `prepare_fn`、`launch_fn`、`userdata` | 3 | 调用方 | C++ 在回放时反复调用（u2-l3） |
| 输出缓冲 | `metric_values`、`metric_value_count` | 2 | 调用方分配 | C++ 只写 |
| 错误通道 | `error`、`error_len` | 2 | 调用方分配 | C++ 只写 |

参数表之外还有一个「第 13 项」：返回值 `int`——0 表示成功、1 表示失败。规划稿标题里的「13 个参数」多半就是把它误计了进去，但它不是参数，是契约的另一半。
