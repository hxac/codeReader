# 日志、调试与环境变量

## 1. 本讲目标

FasterTransformer（以下简称 FT）是一个用 CUDA 异步执行 kernel 的高性能推理库。kernel 一旦启动就立刻把控制权交还给 CPU，错误不会立刻抛出，多 GPU 下日志还会被每个 rank 重复打印——这让「定位问题」和「看懂运行过程」变得困难。

本讲解决三件事：

1. 看懂 FT 的日志系统（`logger.h` / `logger.cc`），知道 `FT_LOG_INFO`、`FT_LOG_DEBUG` 这些宏背后做了什么。
2. 掌握三个最常用的运行期环境变量：`FT_LOG_LEVEL`、`FT_NVTX`、`FT_DEBUG_LEVEL`，知道它们分别在「看日志 / 做性能分析 / 定位 CUDA 错误」时怎么用。
3. 理解为什么「调试模式」会严重拖慢程序，从而养成「只在排查问题时才打开」的习惯。

学完后，你应该能独立写出一条带环境变量的调试命令，并解释它打开后程序行为会发生什么变化。

## 2. 前置知识

阅读本讲前，你需要了解：

- **CUDA 的异步执行模型**：CPU 调用 `cudaMalloc`、启动 kernel（用三尖括号 `<<<>>>` 语法）后，工作是丢给 GPU 的命令队列（stream）去做的，CPU 不会等它跑完。只有调用 `cudaDeviceSynchronize()` 时，CPU 才会阻塞到 GPU 把队列里所有任务做完。
- **异步带来的副作用**：如果一个 kernel 写越界，错误往往要等到「下一次 CPU 主动同步」或「后续某个 CUDA 调用」时才被报告，报错位置和真正出问题的代码常常对不上。
- **环境变量**：操作系统层面的键值对，程序启动时由父进程传给子进程。在 Linux 终端里可以用 `VAR=value ./program` 这种「命令前缀」的方式临时设置，只对这一次运行生效。C++ 里用 `std::getenv("VAR")` 读取。
- **宏（macro）**：C/C++ 预处理器在编译前做的文本替换。`FT_LOG_INFO(...)` 本质上会被替换成一段包含 `if` 判断和函数调用的代码。
- 本讲承接 [u1-l1 项目总览] 中关于 FT 定位的基本认知，建议先读。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/fastertransformer/utils/` 下，它们是贯穿全库的「基础设施」：

| 文件 | 作用 |
| --- | --- |
| [src/fastertransformer/utils/logger.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.h) | 定义 `Logger` 类、日志级别枚举和 `FT_LOG_*` 宏 |
| [src/fastertransformer/utils/logger.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.cc) | `Logger` 构造函数：读取 `FT_LOG_LEVEL` 等环境变量 |
| [src/fastertransformer/utils/nvtx_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nvtx_utils.h) | NVTX 性能标记的接口声明与 `PUSH_RANGE`/`POP_RANGE` 宏 |
| [src/fastertransformer/utils/nvtx_utils.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nvtx_utils.cc) | NVTX 实现：读取 `FT_NVTX`、调用 NVIDIA Tools Extension |
| [src/fastertransformer/utils/cuda_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_utils.h) | `syncAndCheck`：读取 `FT_DEBUG_LEVEL`，决定是否同步检查 CUDA 错误 |
| [README.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md) | `Global Environment` 小节：三个环境变量的官方说明 |

> 提示：`cuda_utils.h` 不在「关键源码」清单里，但它是理解 `FT_DEBUG_LEVEL` 的核心，本讲会补充进来。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲日志系统，再讲 NVTX 性能标记，再讲调试同步，最后把三个环境变量串成一张总表。

### 4.1 日志系统：Logger 类与日志级别

#### 4.1.1 概念说明

几乎所有服务端程序都需要「日志」——把运行过程中的关键信息打到屏幕或文件，方便事后排查。日志一般分级：

- **TRACE / DEBUG**：非常细粒度的信息（比如「进入了某个函数」「某个张量的 shape」），只在排查问题时打开，否则刷屏。
- **INFO**：常规运行信息（比如「模型加载完成」「开始生成」）。
- **WARNING**：可疑但还能继续运行的情况。
- **ERROR**：出错。

FT 设计了一个单例 `Logger` 类来统一管理这些日志。它的核心思想是：**全局维护一个「当前允许输出的最低级别」`level_`，每条日志只在「它自己的级别 ≥ `level_`」时才真正打印**。这样一行环境变量就能控制全库所有日志的详略程度。

#### 4.1.2 核心流程

日志系统的执行流程：

1. 程序启动时，`Logger` 的单例被首次访问，构造函数执行（见 4.1.3 源码）。
2. 构造函数读取环境变量 `FT_LOG_LEVEL`，把字符串（如 `"DEBUG"`）翻译成内部的级别数值，写入 `level_`。
3. 业务代码各处调用 `FT_LOG_INFO("...")`、`FT_LOG_DEBUG("...")` 等宏。
4. 宏先判断「当前级别是否允许」，允许才调用 `Logger::log()`。
5. `log()` 在消息前面拼上 `[FT][INFO]` 这样的前缀，再决定写到 `stdout` 还是 `stderr`。

级别的数值定义如下（数值越小越「啰嗦」，越细粒度）：

\[ \text{TRACE}=0,\ \text{DEBUG}=10,\ \text{INFO}=20,\ \text{WARNING}=30,\ \text{ERROR}=40 \]

判定规则是一个简单的不等式：当 `level_ \leq \text{level}` 时输出。也就是说，把 `level_` 设成 `DEBUG(10)`，那么 `INFO(20)`、`WARNING(30)`、`ERROR(40)` 都满足 `10 \leq 20/30/40`，都会输出；而 `TRACE(0)` 不满足 `10 \leq 0`，被屏蔽。这正是「设成某级别，就看到该级别及更严重」的语义。

#### 4.1.3 源码精读

先看级别枚举，五个级别从细到粗排列：

[logger.h:L30-L36 — 日志级别枚举，数值越小越细粒度](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.h#L30-L36)

```cpp
enum Level {
    TRACE   = 0,
    DEBUG   = 10,
    INFO    = 20,
    WARNING = 30,
    ERROR   = 40
};
```

`Logger` 用「梅耶单例」（Meyers singleton）保证每个线程有一个实例，并禁用拷贝构造和赋值，防止被意外复制：

[logger.h:L38-L44 — getLogger 返回线程局部单例，禁用拷贝](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.h#L38-L44)

注意这里的 `thread_local`：每个 CPU 线程会有自己独立的 `Logger` 实例和独立的 `level_`。在多线程推理（比如 FT 的异步示例）里，这意味着不同线程可以有不同的日志级别——但也意味着构造函数会被每个线程各执行一次。

核心打印逻辑在 `log()` 里。先看带 `rank` 参数的版本（多 GPU 时常用）：

[logger.h:L57-L66 — log() 的级别判定与前缀拼接逻辑](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.h#L57-L66)

```cpp
template<typename... Args>
void log(const Level level, const int rank, const std::string format, const Args&... args)
{
    if (level_ <= level) {                       // 1) 级别判定
        std::string fmt    = getPrefix(level, rank) + format + "\n";
        FILE*       out    = level_ < WARNING ? stdout : stderr;  // 2) 严重级别走 stderr
        std::string logstr = fmtstr(fmt, args...);                // 3) printf 风格格式化
        fprintf(out, "%s", logstr.c_str());
    }
}
```

三个细节值得注意：

- **级别判定** `level_ <= level`：只有当前允许级别 `level_` 不大于本条日志级别时才输出，对应 4.1.2 的不等式。
- **输出流分流** `level_ < WARNING ? stdout : stderr`：`WARNING` 和 `ERROR` 走标准错误流，`INFO`/`DEBUG`/`TRACE` 走标准输出。这样在终端里可以用 `2>` 把错误单独重定向到文件。
- **格式化** `fmtstr(fmt, args...)`：来自 [string_utils.h:L27](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/string_utils.h#L27)，是 `printf` 风格的可变参格式化，所以 `FT_LOG_INFO("step %d", i)` 这种写法是合法的。

带 `rank` 的前缀长这样 `[FT][INFO][0]`，最后那个 `0` 是 GPU 进程号，方便区分是哪张卡打印的：

[logger.h:L103-L106 — 多 rank 前缀，把进程号拼进日志](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.h#L103-L106)

默认日志级别由编译模式决定：

[logger.h:L84-L89 — Debug 构建默认 DEBUG 级别，Release 构建默认 INFO](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.h#L84-L89)

```cpp
#ifndef NDEBUG
const Level DEFAULT_LOG_LEVEL = DEBUG;   // 未定义 NDEBUG（即 Debug 构建）
#else
const Level DEFAULT_LOG_LEVEL = INFO;    // Release 构建
#endif
Level level_ = DEFAULT_LOG_LEVEL;
```

`NDEBUG` 是 C/C++ 标准的「非调试」宏，`assert` 也用它。所以如果你用 `cmake -DCMAKE_BUILD_TYPE=Debug` 编译，默认就是 `DEBUG` 级别，会比 Release 多打很多日志。

业务代码实际使用的，是下面这一组宏。每个宏把固定的级别和可变参数转给 `FT_LOG`：

[logger.h:L109-L120 — FT_LOG 及五个级别宏，业务代码统一用这些宏打日志](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.h#L109-L120)

注意 `FT_LOG` 用了 `do { ... } while (0)` 包裹——这是 C/C++ 宏的标准写法，保证宏展开后是一个完整的语句，能安全地用在 `if` 后面而不产生悬挂 else 问题。宏内部先调用 `getLevel()` 做一次判断，这是为了「级别不满足时连 `log()` 函数都不进」，省掉字符串拼接的开销。

那么 `level_` 是怎么被环境变量改写的？答案在构造函数里：

[logger.cc:L22-L57 — Logger 构造函数读取 FT_LOG_LEVEL 与 FT_LOG_FIRST_RANK_ONLY](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.cc#L22-L57)

关键片段：

```cpp
char* level_name = std::getenv("FT_LOG_LEVEL");          // 读环境变量
if (level_name != nullptr) {
    std::map<std::string, Level> name_to_level = {       // 字符串 → 枚举的映射表
        {"TRACE", TRACE}, {"DEBUG", DEBUG}, {"INFO", INFO},
        {"WARNING", WARNING}, {"ERROR", ERROR},
    };
    auto level = name_to_level.find(level_name);
    // 若 FT_LOG_FIRST_RANK_ONLY=ON，把非 0 号设备的级别强制设成 ERROR
    if (is_first_rank_only && device_id != 0) {
        level = name_to_level.find("ERROR");
    }
    if (level != name_to_level.end()) {
        setLevel(level->second);                          // 写入 level_
    } else {
        fprintf(stderr, "[FT][WARNING] Invalid logger level ...");  // 非法值警告
    }
}
```

这里有三个要点：

1. 构造函数开头还读了一个 `FT_LOG_FIRST_RANK_ONLY`（[logger.cc:L24-L26](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.cc#L24-L26)）：当它为 `ON` 时，非 0 号 GPU 的日志级别被强制压到 `ERROR`，避免多卡时每个 rank 都打印一遍同样的 INFO 信息而刷屏。这是多 GPU 调试时非常实用的开关。
2. 环境变量是**字符串**，需要一张 `name_to_level` 映射表把它翻译成枚举。如果用户填了一个拼错的值（比如 `"DBG"`），不会崩溃，而是打一条 WARNING 并沿用默认级别（[logger.cc:L49-L54](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.cc#L49-L54)）。
3. `setLevel` 内部除了赋值，还会主动 `log(INFO, "Set logger level by %s", ...)` 打一条确认信息（[logger.h:L68-L72](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.h#L68-L72)），方便你确认环境变量真的生效了。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，搞清楚「设了 `FT_LOG_LEVEL=DEBUG` 之后，程序会多打印哪些信息」。

**操作步骤**：

1. 打开 [logger.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.h)，确认五个级别数值。
2. 在仓库里全局搜索 `FT_LOG_INFO(` 和 `FT_LOG_DEBUG(`，分别数一下大概各有多少处调用（用编辑器或 `grep -rn "FT_LOG_DEBUG" src/ | wc -l`）。
3. 对照 4.1.2 的不等式，推断：默认 Release 构建（`level_=INFO`）下，`FT_LOG_DEBUG` 会不会输出？

**需要观察的现象**：

- `FT_LOG_DEBUG` 的调用点数量应该远多于 `FT_LOG_INFO`，因为 DEBUG 是「细粒度」日志，开发者在很多函数入口都埋了点。
- 默认 Release 构建下 `level_=20(INFO)`，而 `FT_LOG_DEBUG` 的级别是 `10`，判定 `20 <= 10` 为假，所以**不输出**——这就是为什么平时跑程序日志不多，但开了 DEBUG 就会刷屏。

**预期结果**：你能口述出「DEBUG 日志在默认 Release 构建下被屏蔽，设 `FT_LOG_LEVEL=DEBUG` 才会输出」。如果暂时无法编译运行，标记「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果用户把环境变量写成 `FT_LOG_LEVEL=dbg`（拼错了），程序会怎样？
**答案**：构造函数里 `name_to_level.find("dbg")` 找不到，进入 else 分支，向 `stderr` 打印一条 `[FT][WARNING] Invalid logger level FT_LOG_LEVEL=dbg. Ignore ...`，然后沿用默认级别（Release 下是 INFO，Debug 构建下是 DEBUG）。程序不会崩溃。

**练习 2**：为什么 `log()` 把 `WARNING`/`ERROR` 写到 `stderr` 而不是 `stdout`？
**答案**：Unix 惯例里 `stdout` 是「正常程序输出」，`stderr` 是「诊断信息」。分开后，可以用 `./program > out.log 2> err.log` 把正常输出和错误日志分别存到不同文件，方便排查；也能让下游管道只消费 `stdout` 而不被错误信息污染。

**练习 3**：在 8 卡 GPT 推理时，怎样让屏幕只看到 0 号卡的 DEBUG 日志？
**答案**：设置 `FT_LOG_FIRST_RANK_ONLY=ON` 并 `FT_LOG_LEVEL=DEBUG`。构造函数会把 1~7 号卡的级别强制压成 `ERROR`，只有 0 号卡保留 DEBUG 级别输出。

---

### 4.2 NVTX 性能标记：nvtx_utils

#### 4.2.1 概念说明

当程序跑得比预期慢时，我们需要知道「时间花在哪」。NVTX（NVIDIA Tools Extension）是 NVIDIA 提供的一个轻量级标记库：你在代码里「打标签」（push 一个 range），NVIDIA 的可视化工具（如 **Nsight Systems**）就能在时间轴上把这个区间显示成一个色块，从而看出哪段代码占用了多少 GPU 时间。

NVTX 标记本身几乎零开销——它只是往一个缓冲区里写事件，不阻塞 GPU。但前提是你得在代码里埋好标记。FT 用 `PUSH_RANGE` / `POP_RANGE` 两个宏把关键区间（比如一次 attention、一次 GEMM）包起来。

关键问题：如果每次推理都打标记，正常运行的日志会被淹没；而且如果用户根本没开 Nsight，标记也没意义。所以 FT 用环境变量 `FT_NVTX` 来**运行期**开关这个功能。

#### 4.2.2 核心流程

1. 编译期：CMake 选项 `USE_NVTX`（默认 `ON`）决定是否把 NVTX 相关代码编进去。只有 `USE_NVTX=ON`，才会定义 `USE_NVTX` 宏并链接 `-lnvToolsExt`。
2. 运行期：业务代码调用 `PUSH_RANGE("attention")`。
3. 宏内部调用 `isEnableNvtx()`，它**首次调用时**读取 `FT_NVTX` 环境变量，缓存结果。
4. 若 `FT_NVTX=ON` 且编译期启用了 NVTX，才真正调用 NVIDIA 的 `nvtxRangePushEx` 打标记。
5. 对应的 `POP_RANGE` 结束这个区间。

#### 4.2.3 源码精读

先看头文件里的接口和宏。整个 NVTX 功能放在 `ft_nvtx` 命名空间下：

[nvtx_utils.h:L19-L35 — ft_nvtx 命名空间的接口声明](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nvtx_utils.h#L19-L35)

业务代码真正用的是下面两个宏，它们都被 `isEnableNvtx()` 守卫：

[nvtx_utils.h:L37-L49 — PUSH_RANGE / POP_RANGE 宏，受 isEnableNvtx() 控制](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nvtx_utils.h#L37-L49)

```cpp
#define PUSH_RANGE(name)                  \
    {                                     \
        if (ft_nvtx::isEnableNvtx()) {    \
            ft_nvtx::ftNvtxRangePush(name);\
        }                                 \
    }

#define POP_RANGE                          \
    {                                      \
        if (ft_nvtx::isEnableNvtx()) {     \
            ft_nvtx::ftNvtxRangePop();     \
        }                                  \
    }
```

注意宏外层用了裸花括号 `{ }`（不是 `do-while`），这样 `PUSH_RANGE(x); POP_RANGE;` 各自成为一个独立块。注意它们没有以分号结尾在宏定义里，调用方写 `PUSH_RANGE("attn");` 时分号在调用处。

运行期开关的核心是 `isEnableNvtx()`，它用「懒读取 + 缓存」模式读环境变量：

[nvtx_utils.cc:L59-L67 — isEnableNvtx 首次调用读取 FT_NVTX 并缓存](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nvtx_utils.cc#L59-L67)

```cpp
bool isEnableNvtx()
{
    if (!has_read_nvtx_env) {                       // 只读一次
        static char* ft_nvtx_env_char = std::getenv("FT_NVTX");
        is_enable_ft_nvtx = (ft_nvtx_env_char != nullptr
                             && std::string(ft_nvtx_env_char) == "ON") ? true : false;
        has_read_nvtx_env = true;                   // 标记已读，后续直接用缓存值
    }
    return is_enable_ft_nvtx;
}
```

这里有个值得学习的工程技巧：`has_read_nvtx_env` 是个静态 bool，保证 `getenv` 只在第一次调用时执行一次。因为 `PUSH_RANGE` 可能在一帧推理里被调用成千上万次，如果每次都 `getenv` 会带来不必要的开销。`getenv` 本身要查环境块，虽然不慢，但在热路径上能省则省。

真正的 NVTX 调用被 `#ifdef USE_NVTX` 包起来，这样即使运行期 `FT_NVTX=ON`，如果编译时没开 `USE_NVTX`，也不会报链接错误：

[nvtx_utils.cc:L69-L80 — ftNvtxRangePush 调用 NVIDIA 的 nvtxRangePushEx](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nvtx_utils.cc#L69-L80)

```cpp
void ftNvtxRangePush(std::string name)
{
#ifdef USE_NVTX
    nvtxStringHandle_t    nameId      = nvtxDomainRegisterStringA(NULL, (getScope() + name).c_str());
    nvtxEventAttributes_t eventAttrib = {0};
    eventAttrib.messageType           = NVTX_MESSAGE_TYPE_REGISTERED;
    eventAttrib.message.registered    = nameId;
    eventAttrib.payloadType           = NVTX_PAYLOAD_TYPE_INT32;
    eventAttrib.payload.iValue        = getDeviceDomain();   // 把 GPU id 作为 payload
    nvtxRangePushEx(&eventAttrib);
#endif
}
```

可以看到标记的「名字」是 `getScope() + name` 拼起来的（比如 `decoder/layer0/attention`），`getDeviceDomain()` 把当前 GPU 号塞进 payload，这样在 Nsight 里能区分不同卡。

最后确认编译期的开关。`USE_NVTX` 是个默认 `ON` 的 CMake option：

[CMakeLists.txt:L118-L122 — USE_NVTX 默认开启，开启后定义 -DUSE_NVTX 宏](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L118-L122)

并且会链接 `nvToolsExt` 库（[CMakeLists.txt:L426-L428](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L426-L428)）。所以默认编译出来的 FT 是「带 NVTX 能力」的，只等运行期 `FT_NVTX=ON` 去激活。

> 小结：`FT_NVTX` 是「运行期开关」，`USE_NVTX` 是「编译期能力」。两者都为真，标记才会真正打出来。

#### 4.2.4 代码实践

**实践目标**：理解 `FT_NVTX=ON` 如何改变程序的可观测性。

**操作步骤**：

1. 在仓库里搜索 `PUSH_RANGE` 的使用点（例如 `grep -rn "PUSH_RANGE" src/ | head -20`），看看 FT 在哪些关键区间打了标记（典型：attention、FFN、layernorm、整个 forward）。
2. 设想这样一个工作流：用 Nsight Systems 抓取一次推理，命令大致是（**示例命令**，具体语法以 nsys 版本为准）：
   ```bash
   FT_NVTX=ON nsys profile -t cuda,nvtx ./bin/bert_example 32 12 32 12 64 1 0
   ```
3. 然后用 `nsys stats` 或 Nsight Systems GUI 打开生成的 `.qdrep` 文件，观察时间轴。

**需要观察的现象**：

- 不加 `FT_NVTX=ON` 时，Nsight 时间轴上只有 CUDA kernel 的色块，没有带名字的 NVTX range，你很难知道「这一堆 kernel 属于 attention 还是 FFN」。
- 加了 `FT_NVTX=ON` 后，时间轴上会出现嵌套的、带 `decoder/...` 名字的区间，把 kernel 分组归类。

**预期结果**：你能解释「为什么做性能分析时必须 `FT_NVTX=ON`」——因为它把「无名的 kernel 序列」变成了「有语义的阶段」。如果本地没有 GPU 或没装 Nsight，标记「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果编译时用 `cmake -DUSE_NVTX=OFF ..`，运行时设 `FT_NVTX=ON` 还会有 NVTX 标记吗？
**答案**：不会。`USE_NVTX=OFF` 时不会定义 `USE_NVTX` 宏，`ftNvtxRangePush` 内部的 `nvtxRangePushEx` 调用被预处理器删除，函数变成空壳。即使 `isEnableNvtx()` 返回 true，也什么都不会发生（且不需要链接 `nvToolsExt`）。

**练习 2**：为什么 `isEnableNvtx()` 要缓存环境变量，而 `Logger` 构造函数里的 `FT_LOG_LEVEL` 不缓存？
**答案**：因为 `Logger` 是单例，构造函数**每个线程只执行一次**，天然就是「读一次」；而 `isEnableNvtx()` 是普通函数，会在每次 `PUSH_RANGE` 时被调用（热路径，每帧上千次），所以必须手动用 `has_read_nvtx_env` 做一次性缓存来避免重复 `getenv`。

---

### 4.3 调试模式：FT_DEBUG_LEVEL 与 syncAndCheck

#### 4.3.1 概念说明

这是三个环境变量里**最危险**的一个，也是排查 CUDA 错误最有用的一个。

回顾 4.2 里的异步模型：kernel 启动后 CPU 立刻返回，错误被推迟报告。假设第 100 行的 kernel 写越界了，错误可能在第 200 行的某个 `cudaMemcpy` 才冒出来，于是堆栈指向第 200 行——你盯着 200 行看半天也看不出问题。

解决办法是「强迫 CPU 等 GPU」：在每个 kernel 之后调用 `cudaDeviceSynchronize()`，让 GPU 把队列跑完，再立刻 `cudaGetLastError()` 检查。这样错误就会在「真正出错的那个 kernel 之后」立刻暴露，定位精准。

代价是：`cudaDeviceSynchronize()` 把「CPU 提交 kernel、GPU 异步执行」的流水线完全打破，CPU 必须干等 GPU 跑完才能提交下一个。性能可能下降几倍甚至几十倍。所以它**只能用于调试**。

#### 4.3.2 核心流程

1. FT 的 kernel 调用点后面通常跟一个 `sync_check_cuda_error()` 宏调用。
2. 这个宏展开成 `syncAndCheck(__FILE__, __LINE__)`。
3. `syncAndCheck` 内部读 `FT_DEBUG_LEVEL`：
   - 若值为 `"DEBUG"`，执行 `cudaDeviceSynchronize()` + `cudaGetLastError()`，有错就抛异常。
   - 否则跳过（保持异步）。
4. 另外，**不论环境变量如何**，只要编译时没定义 `NDEBUG`（即 Debug 构建），也会强制同步检查。

#### 4.3.3 源码精读

核心函数在 `cuda_utils.h`：

[cuda_utils.h:L127-L154 — syncAndCheck：FT_DEBUG_LEVEL=DEBUG 时在每个 kernel 后同步检查错误](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_utils.h#L127-L154)

```cpp
inline void syncAndCheck(const char* const file, int const line)
{
    // When FT_DEBUG_LEVEL=DEBUG, must check error
    static char* level_name = std::getenv("FT_DEBUG_LEVEL");
    if (level_name != nullptr) {
        static std::string level = std::string(level_name);
        if (level == "DEBUG") {
            cudaDeviceSynchronize();                 // 阻塞 CPU 直到 GPU 完成
            cudaError_t result = cudaGetLastError(); // 抓最近一次 CUDA 错误
            if (result) {
                throw std::runtime_error(... + file + ":" + std::to_string(line) + ...);
            }
            FT_LOG_DEBUG(fmtstr("run syncAndCheck at %s:%d", file, line));
        }
    }

#ifndef NDEBUG
    // Debug 构建下，无视环境变量也强制同步
    cudaDeviceSynchronize();
    cudaError_t result = cudaGetLastError();
    if (result) { throw ...; }
#endif
}
```

几个要点：

- **`static` 局部变量**：`static char* level_name` 和 `static std::string level` 用了静态局部变量，意味着 `getenv` 只在第一次调用时执行一次，之后复用——和 4.2 的 `has_read_nvtx_env` 是同一个套路，因为 `syncAndCheck` 也是热路径。
- **错误抛异常**：检测到错误时抛 `std::runtime_error`，并把 `file:line`（即 `sync_check_cuda_error()` 宏所在的源码位置）拼进消息，这正是「精确定位」的关键。
- **双重触发**：除了 `FT_DEBUG_LEVEL=DEBUG`，`#ifndef NDEBUG` 块在 Debug 构建下也会同步。所以 Debug 构建（`CMAKE_BUILD_TYPE=Debug`）即使不设环境变量，也会自动进入慢速精确报错模式——这和 4.1.3 里 Debug 构建默认 `DEBUG` 日志级别是一致的设计。

调用方使用的宏是这样定义的（紧接在 `syncAndCheck` 之后）：

[cuda_utils.h:L154 — sync_check_cuda_error 宏，自动注入当前文件名和行号](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_utils.h#L154)

```cpp
#define sync_check_cuda_error() syncAndCheck(__FILE__, __LINE__)
```

`__FILE__` 和 `__LINE__` 是预定义宏，在预处理时被替换成当前源码文件名和行号。所以每个 `sync_check_cuda_error()` 调用点都能在报错时报告自己的位置。

#### 4.3.4 代码实践

**实践目标**：体会 `FT_DEBUG_LEVEL=DEBUG` 对程序行为的两种影响——「报错位置变准」和「速度变慢」。

**操作步骤**：

1. 在仓库里搜索 `sync_check_cuda_error()`，看看它在多少处被调用（典型出现在各 kernel 入口、模型 forward 关键节点）。
2. 对照 4.3.3，回答：默认情况下（`FT_DEBUG_LEVEL` 未设 + Release 构建）这个宏展开后实际做了什么？
3. 设计一个对比实验（**待本地验证**，需要 GPU）：
   ```bash
   # 不开调试：测速
   time ./bin/bert_example 32 12 32 12 64 1 0
   # 开调试同步：再测速
   time FT_DEBUG_LEVEL=DEBUG ./bin/bert_example 32 12 32 12 64 1 0
   ```

**需要观察的现象**：

- 第二条命令的耗时应该**显著大于**第一条（可能数倍到数十倍），因为每次 kernel 后都插入了同步。
- 如果程序里有越界错误，第二条命令的报错堆栈会比第一条更接近真正的出错点。

**预期结果**：你能口述「`FT_DEBUG_LEVEL=DEBUG` 牺牲性能换取精确的错误定位，仅用于排查问题」。无 GPU 环境则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 README 强调 `FT_DEBUG_LEVEL`「should be used only for debugging」？
**答案**：因为它在每个 kernel 后插入 `cudaDeviceSynchronize()`，彻底破坏了「CPU 提交 + GPU 异步执行」的流水线，CPU 要空等 GPU。这会让吞吐量下降数倍到数十倍，无法用于生产或正式 benchmark。

**练习 2**：`FT_DEBUG_LEVEL` 设成 `"INFO"` 会怎样？
**答案**：`syncAndCheck` 里只判断 `level == "DEBUG"`，`"INFO"` 不匹配，所以**不会**触发同步检查（保持异步）。它不像 `FT_LOG_LEVEL` 那样有一张完整的级别表，只认 `"DEBUG"` 这一个值。但若同时是 Debug 构建（`NDEBUG` 未定义），仍会因 `#ifndef NDEBUG` 块而强制同步。

**练习 3**：`static char* level_name` 改成普通局部变量（去掉 `static`）会有什么坏处？
**答案**：每次调用 `syncAndCheck` 都要重新 `getenv("FT_DEBUG_LEVEL")`。该函数在 `FT_DEBUG_LEVEL=DEBUG` 时每个 kernel 后都会调用，热路径上频繁 `getenv` 会进一步拖慢本就缓慢的调试模式，纯属浪费。

---

### 4.4 三个环境变量总览与配合使用

#### 4.4.1 概念说明

把前面三节合到一起：FT 提供三个运行期环境变量，分别服务于「看日志」「做 profile」「查 CUDA 错误」三个目的。它们互不冲突，可以同时打开。官方说明集中在 README 的 `Global Environment` 小节。

#### 4.4.2 核心流程：总表

| 环境变量 | 取值 | 作用 | 影响源码 | 性能影响 |
| --- | --- | --- | --- | --- |
| `FT_LOG_LEVEL` | `TRACE`/`DEBUG`/`INFO`/`WARNING`/`ERROR` | 控制日志输出最低级别 | [logger.cc:L31-L56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.cc#L31-L56) | TRACE/DEBUG 时大量打印，变慢 |
| `FT_LOG_FIRST_RANK_ONLY` | `ON` | 仅 0 号卡输出详细日志，其余压到 ERROR | [logger.cc:L24-L26](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/logger.cc#L24-L26) | 减少多卡重复输出，略提速 |
| `FT_NVTX` | `ON` | 插入 NVTX 标记，配合 Nsight Systems 做性能分析 | [nvtx_utils.cc:L59-L67](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/nvtx_utils.cc#L59-L67) | 极小，可忽略 |
| `FT_DEBUG_LEVEL` | `DEBUG` | 每个 kernel 后 `cudaDeviceSynchronize()` 检查错误 | [cuda_utils.h:L127-L152](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_utils.h#L127-L152) | 严重拖慢，仅调试用 |

#### 4.4.3 源码精读

官方对这三个变量的说明在 README：

[README.md:L106-L112 — Global Environment 小节，官方对三个环境变量的说明](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L106-L112)

逐条对照源码：

- 第 1 条 `FT_LOG_LEVEL`：README 提醒「level 低于 DEBUG 时会打印大量信息、程序变慢」——这正对应 4.1 里 TRACE/DEBUG 级别埋点极多的事实。
- 第 2 条 `FT_NVTX`：README 给的例子是 `FT_NVTX=ON ./bin/gpt_example`，这正是 4.2 里 `isEnableNvtx()` 识别的 `ON` 值。
- 第 3 条 `FT_DEBUG_LEVEL`：README 明确说「会显著影响性能，只用于调试」，对应 4.3 的同步代价。

#### 4.4.4 代码实践

**实践目标**：写一条「同时打开 DEBUG 日志 + NVTX + 调试同步」的调试命令，并理解每一项。

**操作步骤**：在 Linux 终端里，多个环境变量可以写在同一条命令前：

```bash
FT_LOG_LEVEL=DEBUG FT_NVTX=ON FT_DEBUG_LEVEL=DEBUG FT_LOG_FIRST_RANK_ONLY=ON \
    ./bin/multi_gpu_gpt_example ../../examples/cpp/multi_gpu_gpt/gpt_config.ini
```

逐项解释：

- `FT_LOG_LEVEL=DEBUG`：把日志级别设为 DEBUG，能看到 `FT_LOG_DEBUG` 的细粒度输出（比如 `run syncAndCheck at xxx.cc:NN`）。
- `FT_NVTX=ON`：启用 NVTX 标记，方便用 Nsight Systems 看时间轴。
- `FT_DEBUG_LEVEL=DEBUG`：每个 kernel 后同步检查 CUDA 错误，报错位置最准。
- `FT_LOG_FIRST_RANK_ONLY=ON`：多 GPU 时只让 0 号卡打 DEBUG 日志，避免重复刷屏。

**需要观察的现象**：

- 启动后应先看到 `[FT][INFO][0] Set logger level by DEBUG`（来自 `setLevel`，证明日志级别生效）。
- 运行中会看到大量 `run syncAndCheck at ...` 的 DEBUG 日志。
- 程序明显比平时慢。

**预期结果**：你能独立组合出针对不同场景的命令——「只看日志」用 `FT_LOG_LEVEL`，「做 profile」用 `FT_NVTX`，「查错误」用 `FT_DEBUG_LEVEL`，多卡再加 `FT_LOG_FIRST_RANK_ONLY`。无 GPU 环境标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果只想确认程序「有没有 CUDA 错误」，但不想被海量 DEBUG 日志干扰，应该怎么设？
**答案**：只设 `FT_DEBUG_LEVEL=DEBUG`，不设 `FT_LOG_LEVEL`（保持默认 INFO）。这样每个 kernel 后仍会同步检查错误，但不会打印 `run syncAndCheck` 那类 DEBUG 日志。注意：`syncAndCheck` 里的 `FT_LOG_DEBUG` 在 INFO 级别下不输出，但同步检查本身仍会执行（因为它在 `FT_LOG_DEBUG` 之前）。

**练习 2**：做正式性能 benchmark 时，这三个变量应该怎么设？
**答案**：全部不设（保持默认）。尤其不能用 `FT_DEBUG_LEVEL=DEBUG`（严重拖慢）和 `FT_LOG_LEVEL=TRACE/DEBUG`（打印开销），`FT_NVTX` 虽然开销极小，但严格 benchmark 时也建议关闭以排除任何干扰。

---

## 5. 综合实践

**任务**：你是 FT 的新手，刚跑通一个 GPT 示例，但发现「结果对不上、怀疑某个 kernel 写越界、而且程序偶尔报一个看不出位置的 CUDA 错误」。请用本讲学的三个环境变量，设计一个分步排查流程，并解释每一步用哪个变量、为什么。

**参考思路**：

1. **第一步：精确定位 CUDA 错误**。
   用 `FT_DEBUG_LEVEL=DEBUG ./bin/...` 重跑。因为每个 kernel 后都同步检查，错误会停在真正出问题的 kernel 附近，看异常消息里的 `file:line`。代价是慢，但这是「定位」阶段，不在乎速度。

2. **第二步：理解程序运行过程**。
   定位到某个层后，用 `FT_LOG_LEVEL=DEBUG FT_LOG_FIRST_RANK_ONLY=ON ./bin/...` 打开细粒度日志，观察该层的输入输出 shape、走了哪个分支。多卡时加 `FT_LOG_FIRST_RANK_ONLY=ON` 避免重复输出。

3. **第三步：性能分析**。
   修完 bug 后，如果想看「时间花在哪」，用 `FT_NVTX=ON` 配合 `nsys profile` 抓时间轴，确认 attention/FFN/layernorm 各占多少时间。

4. **第四步：回归正常**。
   排查完毕，**不设**任何这些变量再跑一次，确认性能恢复正常。

**产出**：把上述流程写成一份「FT 调试小抄」（一段文字即可），包含四个阶段各用的命令模板。这个综合实践把「日志级别判定」「NVTX 标记」「同步检查代价」三个知识点串成了一条完整的排查链路。

> 说明：以上命令模板基于本讲源码分析得出。如果你没有 GPU 环境，无法实际运行，请标注「待本地验证」，但仍应能准确写出命令并解释每个参数的含义与依据（对应到具体源码行）。

## 6. 本讲小结

- FT 用单例 `Logger` + 五级日志枚举（TRACE/DEBUG/INFO/WARNING/ERROR）统一管理日志，业务代码用 `FT_LOG_*` 宏打日志，判定规则是 `level_ <= level`。
- `FT_LOG_LEVEL` 在运行期决定日志最低级别；`Logger` 构造函数用一张字符串→枚举映射表翻译它，非法值会降级为 WARNING 并沿用默认级别。
- `FT_NVTX`（配合编译期 `USE_NVTX`）控制是否插入 NVTX 性能标记，配合 Nsight Systems 做时间轴分析，开销极小。
- `FT_DEBUG_LEVEL=DEBUG` 在每个 kernel 后插入 `cudaDeviceSynchronize()` + 错误检查，能精确定位 CUDA 错误，但严重拖慢性能，仅用于调试。
- Debug 构建（`NDEBUG` 未定义）会同时默认 DEBUG 日志级别并强制同步检查——这是 FT「Debug 构建自带全调试」的一致设计。
- 热路径上的环境变量读取都用 `static` 局部变量做一次性缓存（`has_read_nvtx_env`、`static level_name`），避免重复 `getenv`。

## 7. 下一步学习建议

本讲讲的是「怎么观察和调试 FT」，但还没进入 FT 真正的数据结构。建议下一步：

- **进入 Unit 2 核心基础设施**：先读 [u2-l1 统一张量抽象：Tensor / TensorMap / DataType]，理解 FT 里所有模型接口都依赖的 `Tensor` 类。在那里你会再次看到 `FT_LOG` 宏在断言和错误处理中的实际使用。
- **想多 GPU 调试更顺手**：可以跳到 [u7-l1 张量并行] 看 `NcclParam` 与 rank 的关系，回来更能体会 `FT_LOG_FIRST_RANK_ONLY` 解决的「多卡日志重复」问题有多实际。
- **延伸阅读源码**：直接打开 [string_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/string_utils.h) 看 `fmtstr` 的实现，理解 `FT_LOG_INFO("step %d", i)` 这种 printf 风格格式化是怎么做到类型安全的。
