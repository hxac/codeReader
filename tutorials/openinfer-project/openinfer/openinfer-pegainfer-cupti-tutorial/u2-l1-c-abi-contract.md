# C ABI 契约：pegainfer_cupti_profile_range 的 12 个参数

> **勘误**：规划大纲里本讲的标题写的是「13 个参数」。逐行数过源码之后确认是 **12 个**——这与 u1-l1 讲过的「12 个 C ABI 裸参数」一致。文件名沿用大纲不变，正文一律以源码为准。这个小小的出入本身就是 FFI 工作的第一课：**契约不是写在文档里的，是写在签名里的**。

## 1. 本讲目标

学完本讲，你应该能够：

1. 默写 `pegainfer_cupti_profile_range` 的 12 个参数，并按「输入上下文 / 输入配置 / 回调三元组 / 输出缓冲 / 错误通道」五组分类；
2. 解释「调用方预分配输出与错误缓冲 + `write_error` 截断写入」这套配合的每个细节：为什么永远留一个字节给 `\0`、截断发生在哪一侧、成功时错误缓冲里是什么；
3. 识别函数入口处的四道防御性校验（null context、null range 名、空指标列表、null 指标/输出指针）及其先后顺序，并解释为什么它们排在任何 CUDA / CUPTI 初始化之前；
4. 把 C++ 侧的每个 C 类型对应到 Rust 侧 `lib.rs` 里的类型别名与传参语句上，理解 `status` 与错误合并的三分支。

## 2. 前置知识

- **ABI（Application Binary Interface，应用二进制接口）**：两段已编译代码之间的二进制约定——参数走寄存器还是栈、返回值放哪、符号名长什么样。C ABI 是所有语言之间的「普通话」，Rust 与 C++ 互调时双方都退回这套约定。
- **名字修饰（name mangling）**：C++ 编译器会把函数名编码成携带类型信息的符号。`extern "C"` 告诉编译器：这个函数用 C 规则命名、用 C 调用约定。这样 Rust 侧才能用 `pegainfer_cupti_profile_range` 这个裸名字找到它。
- **异常绝不跨 FFI**：C++ 的异常和 Rust 的 panic 都依赖各自运行时的栈展开（unwinding），另一边的运行时完全不认识。异常穿过 `extern "C"` 边界是未定义行为。所以本 crate 的约定是：C++ 在函数内部 `catch` 一切，把异常压成一个字符串，通过错误缓冲回传。
- **只能传「双方都懂」的类型**：`std::string`、`String`、`Vec` 的内存布局是各语言标准库的私有实现，对岸无法解读。跨边界只能用裸指针、整数长度、原始数组、函数指针。
- **调用方分配缓冲（caller-allocated buffer）**：由调用方分配内存并把指针和容量一起传入，被调方只往里写。好处是不需要「被调方分配 + 调用方释放」的第二套协议，也不会让两套 allocator 混用。
- **int 状态码 + 错误字符串**：`int` 返回值给程序判断（0 成功 / 1 失败），字符串缓冲给人读。这是 `errno` + `perror`、CUDA `cuGetErrorString` 一脉相承的经典模式。
- 承接 u1-l1：pegainfer-cupti 只有四个文件，Rust 侧唯一公开函数是 `profile_range_with_prepare`，它把 12 个 C ABI 裸参数收敛成带闭包的安全签名；本讲钻进那条边界本身。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲聚焦的行 |
| --- | --- | --- |
| `pegainfer-cupti/csrc/range_profiler.cpp`（402 行） | C++ 实现，CUPTI 会话全流程 | L18 回调类型别名；L25-L32 `write_error`；L358-L402 导出函数 |
| `pegainfer-cupti/src/lib.rs`（145 行） | Rust 封装 | L7-L8 类型别名；L10-L25 `unsafe extern` 声明；L85-L145 公开函数与错误合并 |
| `pegainfer-cupti/build.rs`（34 行） | 构建脚本 | L20 静态库名 `pegainfer_cupti_range_profiler`（综合实践链接时要用） |

## 4. 核心概念与源码讲解

### 4.1 extern "C" 导出函数完整签名

#### 4.1.1 概念说明

整个 crate 向外界只导出一个符号：`pegainfer_cupti_profile_range`。它就是 Rust 世界与 CUPTI 世界之间唯一的门——所有输入（context、指标清单、回调）、所有输出（指标值、错误文本）都从这 12 个参数里过。理解了这条签名，就理解了这个库的能力边界：单区间（range）、硬件计数器采样、调用方自带工作闭包。

#### 4.1.2 核心流程

12 个参数按职责分成五组，方向指的是数据流向（调用方 → 被调方，或反过来）：

| 分组 | 参数 | 方向 | 说明 |
| --- | --- | --- | --- |
| ① 输入上下文 | `context`、`device_index` | 调用方 → 被调方 | 在哪个 CUDA context 的第几块卡上采样 |
| ② 输入配置 | `range_name`、`metric_names`、`metric_count` | 调用方 → 被调方 | 区间名（只是标签）与 NUL 结尾的指标名数组 |
| ③ 回调三元组 | `prepare_fn`、`launch_fn`、`userdata` | 双向 | C++ 在测量循环里通过它们反向调用 Rust 闭包 |
| ④ 输出缓冲 | `metric_values`、`metric_value_count` | 被调方 → 调用方 | 每个指标一个 `f64`，与指标名按位置对应 |
| ⑤ 错误通道 | `error`、`error_len` | 被调方 → 调用方 | NUL 结尾错误字符串 + 容量，配合 `int` 返回值 |

「回调三元组」是本签名里最有味道的一组：C++ 不认识 Rust 闭包，于是 Rust 侧把闭包状态藏进 `userdata`（一个 `*mut c_void`），C++ 只拿着两个函数指针和这根「不透明手柄」在合适的时机回调——这是 u3-l1 的主题，本讲只需记住 `userdata` 归调用方所有、C++ 只透传。

#### 4.1.3 源码精读

先看 C++ 侧回调指针的类型别名——它出现在文件靠前的位置，是签名的一部分：

[csrc/range_profiler.cpp:18](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L18) 定义了 `PegaCuptiCallback = int (*)(void *)`：接收 userdata、返回 int 状态（0 成功）的裸函数指针。

然后是主角——导出函数的完整签名：

[csrc/range_profiler.cpp:358-362](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L358-L362) 用 `extern "C"` 导出 `pegainfer_cupti_profile_range`，12 个参数依次是 context、device_index、range_name、metric_names、metric_count、prepare_fn、launch_fn、userdata、metric_values、metric_value_count、error、error_len，返回 int。

```cpp
extern "C" int pegainfer_cupti_profile_range(
    CUcontext context, size_t device_index, const char *range_name,
    const char **metric_names, size_t metric_count, PegaCuptiCallback prepare_fn,
    PegaCuptiCallback launch_fn, void *userdata, double *metric_values,
    size_t metric_value_count, char *error, size_t error_len) {
```

Rust 侧对这 12 个参数的声明一一对应（详见 4.4）：

[src/lib.rs:10-25](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L10-L25) 在 `unsafe extern "C"` 块中声明同名函数，参数名与 C++ 侧完全一致，方便逐行对照。

注意签名里没有任何「长度未知的数组」——每个指针参数要么自带长度（`metric_count`、`metric_value_count`、`error_len`），要么以 NUL 结尾（`range_name`、`metric_names` 里的每个名字）。**跨边界的数据永远自带尺寸说明**，这是防止越界读写的第一道设计保障。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立对 12 个参数的「所有权直觉」——谁分配、谁写、谁只读。
2. **操作步骤**：把 [csrc/range_profiler.cpp:358-362](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L358-L362) 的签名抄进笔记，为每个参数标注两列：`读/写/双向`、`调用方所有 / 被调方所有`。
3. **需要观察的现象**：标注时你会发现 C++ 实现从不分配任何要交给调用方释放的东西——所有缓冲都是调用方带进来的。
4. **预期结果**：context、device_index、range_name、metric_names、metric_count 与三个回调参数为「调用方所有，C++ 只读」；metric_values 与 error 为「调用方所有，C++ 写入」；没有任何一列是「被调方所有」。

#### 4.1.5 小练习与答案

1. **练习**：如果去掉 `extern "C"`，会发生什么？
   **答案**：C++ 编译器会做名字修饰，导出符号变成携带类型信息的 mangled 名字；Rust 侧 `unsafe extern` 块里用裸名声明的符号在链接期找不到，报 undefined reference。`extern "C"` 的作用就是关闭修饰、统一到 C 调用约定。
2. **练习**：为什么 `context` 和 `device_index` 要分开传？有 context 不就知道在哪块卡上了吗？
   **答案**：`CUcontext` 是不透明句柄，CUPTI 的 `cuptiDeviceGetChipName` 等接口需要的是设备序号而不是 context；context 用来绑定执行环境（构造 `CurrentContextGuard`），device_index 用来查架构信息，两者来源不同，调用方都现成持有，所以各传各的。
3. **练习**：数一数参数到底几个？
   **答案**：12 个。分组记忆：2（上下文）+ 3（配置）+ 3（回调）+ 2（输出）+ 2（错误）。大纲写作 13 是笔误。

### 4.2 入口参数校验与提前失败

#### 4.2.1 概念说明

FFI 函数的第一件事不是干活，而是**验证调用方没骗你**。C++ 实现在函数体最开头放了四道廉价的检查，任何一道不过就立刻抛异常退出——此时 CUPTI 还没初始化、CUDA 还没被碰、GPU 完全没参与。这叫 fail-fast（尽早失败）：把最便宜、最局部的检查放在最前面，失败路径不产生任何副作用，错误消息稳定可 grep。

#### 4.2.2 核心流程

```text
进入 pegainfer_cupti_profile_range
  │ try {
  ├─ ① context == nullptr ?          → 抛 "CUDA context is null"
  ├─ ② range_name == nullptr ?       → 抛 "range name is null"
  ├─ ③ metric_count == 0 ?           → 抛 "metric list is empty"
  ├─ ④ metric_names/metric_values
  │     任一为 nullptr ?              → 抛 "metric input or output buffer is null"
  │ （—— 以上全部通过，才第一次接触 CUDA/CUPTI ——）
  ├─ initialize_cupti_once()          ← 全局一次性 CUPTI 初始化
  ├─ CurrentContextGuard(context)     ← 临时切换当前 context
  └─ …会话装配 → 回放采样 → 解码求值…
  } catch (…异常) { write_error; return 1; }
```

注意顺序细节：③（数量为零）排在 ④（指针为空）之前——空列表时即使指针也是空的，也先报「列表为空」。检查顺序本身就是契约的一部分。

#### 4.2.3 源码精读

[csrc/range_profiler.cpp:363-375](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L363-L375) 是四道入口校验，每条消息都是固定的英文短句：

```cpp
try {
    if (context == nullptr) {
        throw std::runtime_error("CUDA context is null");
    }
    if (range_name == nullptr) {
        throw std::runtime_error("range name is null");
    }
    if (metric_count == 0) {
        throw std::runtime_error("metric list is empty");
    }
    if (metric_names == nullptr || metric_values == nullptr) {
        throw std::runtime_error("metric input or output buffer is null");
    }
```

[csrc/range_profiler.cpp:377-378](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L377-L378) 紧随其后：`initialize_cupti_once()` 与 `CurrentContextGuard current_context(context)`——这是函数里第一次接触 CUDA/CUPTI 运行时。四道校验全部位于它们之前，这就是「传错参数的失败不需要 GPU」的根据。

还有第五道检查藏在深处，作为对照：[csrc/range_profiler.cpp:324-329](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L324-L329) 里 `evaluate` 会检查 `metric_value_count < metric_count` 并抛 "metric output buffer is smaller than metric list"——它发生在 CUPTI 会话已建好之后，失败成本高得多。入口四道检查的存在，就是为了让最常见的调用错误根本走不到这里。

#### 4.2.4 代码实践（纸面推演型）

1. **实践目标**：不看答案，凭检查顺序预测四种非法输入各自返回哪条错误消息。
2. **操作步骤**：对以下四组输入，先写下你预测的错误字符串，再对照 [csrc/range_profiler.cpp:363-375](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L363-L375) 验证：(a) context 为 null，其余合法；(b) context 非 null、range_name 为 null；(c) context 非 null、range_name 合法、metric_count 为 0；(d) context 非 null、metric_count 为 1、metric_names 为 null。附加题：context 为 null 且 metric_count 为 0 同时发生时报哪条？
3. **需要观察的现象**：你的预测顺序是否与源码一致。
4. **预期结果**：(a) "CUDA context is null"；(b) "range name is null"；(c) "metric list is empty"；(d) "metric input or output buffer is null"；附加题报 (a)——第一条命中的检查直接抛出。第 5 节的 probe 程序会在真实进程里复现 (a) 与 (c)（待本地验证）。

#### 4.2.5 小练习与答案

1. **练习**：为什么把 null 检查放在 `initialize_cupti_once()` 之前很重要？
   **答案**：`initialize_cupti_once` 用 `std::call_once` 全局只执行一次且结果被缓存（u3-l3 展开）。如果先用垃圾参数触发了初始化再失败，进程后续的合法调用也要背上「首次失败已缓存」的后果；先校验能让非法调用对全局状态零影响。
2. **练习**：这四道检查里哪道在「无 GPU 机器」上也能被触发？
   **答案**：全部四道。它们只读指针和整数，位于任何 CUDA Driver / CUPTI 调用之前；这就是综合实践不需要 GPU 的原因。
3. **练习**：`metric_count == 0` 与 `metric_names == nullptr` 哪个先检查？为什么这个顺序合理？
   **答案**：`metric_count == 0` 先检查。零长度列表在语义上已经错误，此时指针是什么已无关紧要；先报更根本的错误，消息也更不容易误导排查者。

### 4.3 write_error 的截断语义

#### 4.3.1 概念说明

`write_error` 是错误通道的写入门卫：把任意长度的 `std::string` 消息压进调用方给的 `char` 缓冲，**保证永远 NUL 结尾、永不越界**。它的两条规约——「留一字节给 NUL」和「放不下就截断」——是 Rust 侧能安全地用 `CStr` 读取错误缓冲的全部依据。

#### 4.3.2 核心流程

对长度为 \(\lvert message\rvert\) 的消息和容量为 error_len 的缓冲：

\[ \text{copy\_len} = \min(\text{error\_len} - 1,\;\lvert message \rvert) \]

随后把消息前 copy_len 字节 memcpy 进缓冲，并在 error[copy_len] 写入 NUL。两个推论：

- 只要 error 非空指针且容量大于 0，缓冲里必然存在 NUL，Rust 用 `CStr::from_ptr` 读取一定安全；
- 消息最多完整放下「容量减一」字节，超出部分被截断——「宁可丢尾巴，不可越界写」。

#### 4.3.3 源码精读

[csrc/range_profiler.cpp:25-32](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L25-L32) 是 `write_error` 的全部 7 行实现：

```cpp
void write_error(char *error, size_t error_len, const std::string &message) {
    if (error == nullptr || error_len == 0) {
        return;
    }
    const size_t copy_len = std::min(error_len - 1, message.size());
    std::memcpy(error, message.data(), copy_len);
    error[copy_len] = '\0';
}
```

第 26-28 行的提前返回意味着调用方可以传「我不关心错误文本」（null 缓冲或零容量），函数照常工作。三个调用点覆盖了函数的三种结局：

- [csrc/range_profiler.cpp:393](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L393)：成功路径调用 `write_error(error, error_len, "")`——copy_len 为 0，只写一个 NUL。也就是说**成功时错误缓冲被清成空字符串**，不是残留垃圾。
- [csrc/range_profiler.cpp:395-397](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L395-L397)：`catch (const std::exception &ex)` 把 `ex.what()` 写入并返回 1——所有 4.2 节的校验异常都从这里出去。
- [csrc/range_profiler.cpp:398-400](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L398-L400)：兜底 `catch (...)` 写死 "unknown CUPTI profiler error"，接住非 `std::exception` 的抛出物。**无论抛出什么，异常都不出这道门**。

Rust 侧的配合在 [src/lib.rs:100-101](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L100-L101)：values 与 4096 字节的 error 缓冲都是**调用方预分配**的——

```rust
let mut values = vec![0.0; metric_ptrs.len()];
let mut error = vec![0; 4096];
```

读取端在 [src/lib.rs:134-136](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L134-L136) 用 `CStr::from_ptr(error.as_ptr())` 加 `to_string_lossy()`——这行代码的安全性完全依赖 `write_error` 的「必有 NUL」承诺；缓冲预填 0 则是第二重保险（即使 C 侧有 bug 什么都没写，零初始化的缓冲依然以 NUL 开头）。

#### 4.3.4 代码实践（纸面推演型）

1. **实践目标**：手工执行一遍截断公式。
2. **操作步骤**：设 error_len = 8、message = "CUDA context is null"（20 字节），计算 copy_len、写入的字节序列、NUL 落在第几位。再用 error_len = 4096 重算一遍。
3. **需要观察的现象**：两种容量下结果的差异。
4. **预期结果**：error_len = 8 时 copy_len = min(7, 20) = 7，缓冲内容为 7 个可见字符 "CUDA co" 加一个 NUL，恰好占满 8 字节；error_len = 4096 时消息完整放入，copy_len = 20。若想在真实进程里验证，把第 5 节 probe 的 error 缓冲改成 8 字节即可（待本地验证）。

#### 4.3.5 小练习与答案

1. **练习**：为什么成功路径也要调用一次 `write_error(error, error_len, "")`？
   **答案**：保证「无论成败，错误缓冲都是合法 C 字符串」这一不变量成立。Rust 侧不区分成败地用 `CStr` 读取缓冲，有了这次清空，成功时读到的是空串而不是上次的残留。
2. **练习**：error_len = 1 时会发生什么？
   **答案**：copy_len = min(0, 消息长度) = 0，任何消息都写不进去，只写 error[0] 为 NUL——错误文本丢失但程序安全。调用方想要可读的错误，就得给足容量。
3. **练习**：截断发生在 C++ 侧还是 Rust 侧？为什么不在 Rust 侧截断？
   **答案**：C++ 侧。写入缓冲的是 C++，只有它知道实际拷了多少字节；如果让它随意写、由 Rust 事后截断，越界写入早已发生。截断必须与写入在同一侧、同一步完成。

### 4.4 Rust 侧 unsafe extern 声明的对应关系

#### 4.4.1 概念说明

`lib.rs` 的前 25 行是这条 C ABI 契约的 Rust 投影：两个类型别名消除了裸指针声明的含糊，一个 `unsafe extern "C"` 块逐参数复述 C++ 签名。注意 crate 用的是 Rust 2024 edition（见 [Cargo.toml:2](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/Cargo.toml#L2)），2024 里 `unsafe extern` 块内声明的函数自动视为 unsafe，调用必须包 `unsafe` 块；老 edition 的写法是 `extern "C"` 加 `unsafe fn`——综合实践里手写 probe 时会用到这个区别。

#### 4.4.2 核心流程

公开函数 `profile_range_with_prepare`（[src/lib.rs:85-92](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L85-L92)）的翻译流水线：

```text
&str / &[&str]            →  CString / Vec<CString>      （L93-L98，内嵌 NUL 会失败）
Vec<CString>              →  Vec<*const c_char>           （L99，指针数组）
闭包 + Option             →  CallbackState + trampoline   （L102-L111，u3-l1 展开）
Vec<f64> / Vec<u8>(4096)  →  as_mut_ptr() + len()         （L100-L101，调用方缓冲）
12 个裸参数 FFI 调用                                        （L113-L128）
status==0 → Ok(values)；否则合并两条错误源                   （L130-L144）
```

#### 4.4.3 源码精读

**类型别名**。[src/lib.rs:7-8](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L7-L8) 定义 `CuContext = *mut c_void`（对应不透明的 `CUcontext`）和 `CuptiCallback = Option<unsafe extern "C" fn(*mut c_void) -> c_int>`（对应 `int (*)(void *)`，`Option` 表达「可为 null 的函数指针」）。

**声明块**。[src/lib.rs:10-25](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L10-L25) 逐参数复述 C++ 签名，参数名一一相同，是两侧对照的锚点。

**C 与 Rust 的类型映射表**（行号指 lib.rs，实参列指调用处 [src/lib.rs:113-128](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L113-L128)）：

| # | C/C++ 参数 | C 类型 | Rust 类型 | Rust 实参（行号） |
| --- | --- | --- | --- | --- |
| 1 | context | CUcontext | CuContext（L7） | context（L115） |
| 2 | device_index | size_t | usize | device_index（L116） |
| 3 | range_name | const char * | *const c_char | range_name.as_ptr()（L117） |
| 4 | metric_names | const char ** | *const *const c_char | metric_ptrs.as_ptr()（L118） |
| 5 | metric_count | size_t | usize | metric_ptrs.len()（L119） |
| 6 | prepare_fn | int (*)(void *) | CuptiCallback（Option，L8） | prepare_fn（L120） |
| 7 | launch_fn | int (*)(void *) | 同上 | Some(launch_trampoline)（L121） |
| 8 | userdata | void * | *mut c_void | 裸指针化的 state（L122） |
| 9 | metric_values | double * | *mut f64 | values.as_mut_ptr()（L123） |
| 10 | metric_value_count | size_t | usize | values.len()（L124） |
| 11 | error | char * | *mut c_char | error.as_mut_ptr()（L125） |
| 12 | error_len | size_t | usize | error.len()（L126） |

**可空回调的双侧配合**。[src/lib.rs:107-111](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L107-L111) 只在调用方真的给了 prepare 闭包时才传 trampoline，否则传 `None`（FFI 上就是 NULL 指针）；C++ 侧对应 [csrc/range_profiler.cpp:282](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L282) 的 `if (prepare_fn != nullptr && ...)` 判空跳过。而 launch 是必需的：Rust 恒传 `Some(launch_trampoline)`，C++ 侧 [csrc/range_profiler.cpp:298](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L298) 把 launch_fn 为 nullptr 当错误抛出。

**status 与错误合并**。[src/lib.rs:130-144](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L130-L144) 先看 status 是否为 0，是则直接 `Ok(values)`；否则读 FFI 错误缓冲，再取出回调暂存的错误，按三分支合并：两者都有 → 拼接为「FFI 错误; callback error: 回调错误」；只有回调错 → 单独返回；只有 FFI 错 → 单独返回。什么时候会「两者都有」？回调返回非 0 时，C++ 会抛 "CUPTI prepare callback failed" 或 "CUPTI launch callback failed" 写进错误缓冲（[csrc/range_profiler.cpp:282-284](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L282-L284)、[csrc/range_profiler.cpp:298-300](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L298-L300)），此时 Rust 侧 `state.error` 里也存了闭包的原始错误——两条信息拼在一起才完整。

另外，[src/lib.rs:79-84](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L79-L84) 的文档注释把 unsafe 前提写成了契约：context 必须是对应 device_index 的活 context，回调必须只在该 context 上发射工作——这是 u3-l3 的伏笔。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：完成「声明 ↔ 调用」的行级连线。
2. **操作步骤**：打开 [src/lib.rs:10-25](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L10-L25) 与 [src/lib.rs:113-128](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L113-L128)，对照上面映射表逐参数核对；再回答：第 5 个参数（metric_count）传的是 `metric_ptrs.len()` 而不是函数入参切片的长度——两者为什么必然相等？
3. **需要观察的现象**：12 个实参中哪些是「现成值直传」，哪些是「先转换再传」。
4. **预期结果**：直传的有 context、device_index；转换后传的有 range_name（CString 化）、metric_names（CString 化加指针数组化）、prepare/launch（闭包变 trampoline）、userdata（state 裸指针化）、values/error（Vec 变裸指针加长度）。`metric_ptrs` 由 `metric_names` 一一映射而来（[src/lib.rs:95-99](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L95-L99)），长度当然相等——但 C 侧无法验证这一点，只能信任调用方，这正是 4.2 节校验如此重要的原因。

#### 4.4.5 小练习与答案

1. **练习**：`CuptiCallback` 为什么是 `Option<...>` 而不是直接函数指针？
   **答案**：C 侧的函数指针天然可为 null，Rust 用 `Option<unsafe extern fn>` 表达同一事实——`None` 在 FFI 上就编码为 NULL 指针。这与 C++ 侧 `prepare_fn != nullptr` 的判空（L282）一一对应，类型系统把「可空」写进了签名。
2. **练习**：status 为 0 时直接 `return Ok(values)`，values 里的值是谁写的？
   **答案**：C++ 侧 `evaluate` 通过 `params.pMetricValues = metric_values`（[csrc/range_profiler.cpp:351](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L351)）把 CUPTI 求值结果按位置写进缓冲；Rust 预填的 0.0 只是占位分配，成功时已被覆盖。
3. **练习**：如果 `profile_range_with_prepare` 的调用方传了内嵌 NUL 的 range 名，错误从哪条路径产生？
   **答案**：走不到 FFI——[src/lib.rs:93-94](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L93-L94) 的 `CString::new` 会先返回 Err（NulError 转成 CuptiProfilerError）。字符串清洗发生在边界之内、任何 GPU 访问之前。

## 5. 综合实践

**任务**：写一个约 40 行的独立 probe 程序（**示例代码**，不进仓库），绕过 `lib.rs` 直接链接静态库 `libpegainfer_cupti_range_profiler.a`，亲手调一次 C ABI，在真实进程里复现 4.2 节的两条入口校验——全程不需要 GPU。

1. **实践目标**：验证「入口校验先于一切 CUDA/CUPTI 初始化」，并亲手体验调用方预分配缓冲的契约。
2. **操作步骤**：
   a. 在有 CUDA 工具链的机器上构建 crate 产物（遵循仓库约定用 `--release`）：

   ```bash
   cargo build --release -p pegainfer-cupti
   ```

   b. 找到 build 脚本产物（workspace 的 target 在仓库根下，静态库名来自 [build.rs:20](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L20) 的 `build.compile("pegainfer_cupti_range_profiler")`）：

   ```bash
   find ../target/release/build -name 'libpegainfer_cupti_range_profiler.a' | head -1
   ```

   c. 把下面的 probe.rs（示例代码）放到临时目录，用 rustc 直接编译成可执行文件（一条命令写完；路径按本机 CUDA 安装调整）：

```rust
// probe.rs —— 示例代码：直接调用 pegainfer-cupti 的 C ABI，观察入口校验
use std::ffi::{c_char, c_int, c_void};

type CuContext = *mut c_void;
type CuptiCallback = Option<unsafe extern "C" fn(*mut c_void) -> c_int>;

extern "C" {
    fn pegainfer_cupti_profile_range(
        context: CuContext, device_index: usize, range_name: *const c_char,
        metric_names: *const *const c_char, metric_count: usize,
        prepare_fn: CuptiCallback, launch_fn: CuptiCallback, userdata: *mut c_void,
        metric_values: *mut f64, metric_value_count: usize,
        error: *mut c_char, error_len: usize,
    ) -> c_int;
}

unsafe extern "C" fn noop_launch(_: *mut c_void) -> c_int { 0 }

fn probe(context: CuContext, metric_count: usize, label: &str) {
    let metric = b"gpu__time_duration.sum";
    let metric_ptrs: [*const c_char; 1] = [metric.as_ptr().cast()];
    let mut values = [0.0f64; 1];
    let mut error = [0u8; 4096];
    let status = unsafe {
        pegainfer_cupti_profile_range(
            context, 0, b"probe".as_ptr().cast(),
            metric_ptrs.as_ptr(), metric_count,
            None, Some(noop_launch), std::ptr::null_mut(),
            values.as_mut_ptr(), values.len(),
            error.as_mut_ptr().cast(), error.len(),
        )
    };
    let text = error.iter().take_while(|&&b| b != 0).map(|&b| b as char).collect::<String>();
    println!("{label}: status = {status}, error = \"{text}\"");
}

fn main() {
    probe(std::ptr::null_mut(), 1, "场景1 null context");
    probe(1usize as *mut c_void, 0, "场景2 空指标列表"); // 假 context 只会被判非空，不会被解引用
}
```

   编译与运行（`<out>` 是步骤 b 找到的目录，CUDA 路径按本机替换）：

   ```bash
   rustc probe.rs --edition 2021 -o probe -L <out> -l static=pegainfer_cupti_range_profiler -L /usr/local/cuda/lib64 -L /usr/local/cuda/extras/CUPTI/lib64 -l cuda -l cupti -l stdc++
   LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/cuda/extras/CUPTI/lib64 ./probe
   ```

3. **需要观察的现象**：两个场景各自打印的 status 与 error；注意场景 2 用假指针也能安全触发——因为 context 只被拿来和 nullptr 比较，抛异常的检查发生在任何解引用之前（这只是观察校验顺序的实验手段，真实代码里禁止伪造 context）。
4. **预期结果**（待本地验证——本讲义写作环境无法运行编译器，以下为按源码推演的结果）：

   ```text
   场景1 null context: status = 1, error = "CUDA context is null"
   场景2 空指标列表: status = 1, error = "metric list is empty"
   ```

5. **进阶变体**：把 error 缓冲从 4096 字节改成 8 字节再跑场景 1，按 4.3 节公式，输出应变为截断后的 "CUDA co"。
6. **无 CUDA 工具链时的替代实践（源码阅读型）**：只允许看 [csrc/range_profiler.cpp:358-402](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L358-L402)，在纸上回答：从函数入口到第一条 CUPTI 调用之间隔着几行代码、几道检查？对照 4.2 节流程图核对。

## 6. 本讲小结

- 契约的实体是 [csrc/range_profiler.cpp:358-362](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L358-L362) 的 **12 个参数**（大纲误写 13，以源码为准），按「上下文 / 配置 / 回调三元组 / 输出缓冲 / 错误通道」五组记忆。
- 入口四道校验（null context、null range 名、空指标列表、null 指标/输出指针）全部位于任何 CUDA/CUPTI 初始化之前——非法输入的失败零副作用、不需要 GPU。
- 错误通道的规约：int 返回值判成败；错误文本由 `write_error` 以 \(\min(\text{error\_len}-1,\;\lvert message\rvert)\) 字节截断写入并**必然 NUL 结尾**，成功时缓冲被清成空串。
- 异常绝不跨界：C++ 顶层 catch（含 `catch (...)` 兜底）把一切压成字符串，这是 Rust 侧敢用 `CStr` 读缓冲的前提。
- Rust 侧用类型别名加 `unsafe extern` 块逐参数镜像 C 签名，`Option<fn>` 表达可空回调，status 之后是「FFI 错误 + 回调错误」的三分支合并。

## 7. 下一步学习建议

本讲只讲了「门」——签名与守卫。下一讲 **u2-l2 会话建立：从 cuptiProfilerInitialize 到 SetConfig 的七步装配线** 将走进门内：入口校验通过之后，`initialize_cupti_once`、`chip_name`、`counter_availability_image`（先查大小再取数据的两次调用惯用法）、`initialize_host`、`create_config_image`、`enable_range_profiler`、`create_counter_data_image`、`set_config` 如何一步步把一个能采样的 Range Profiler 会话装配出来。建议提前通读 [csrc/range_profiler.cpp:78-275](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L78-L275) 的匿名命名空间，带着一个问题读：这七步里哪几步的结果是「image」（一段不透明字节串），为什么要用字节串而不是结构体承载？
