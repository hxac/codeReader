# 接收流与元数据

## 1. 本讲目标

上一讲（u2-l5）我们只看了 `rx_streamer` 的**接口契约**：它有哪些方法、`stream_args_t` 怎么填、`recv`/`send` 的参数长什么样。但接口是"纸面约定"，真正的难点在于：**数据真正进来之后，每一帧样本携带的"元数据"该怎么读、出错时该怎么处理、循环怎么优雅地停下来**。

本讲就以 `rx_samples_to_file` 这个真实示例为骨架，把接收侧彻底讲透。学完后你应当能够：

- 正确调用 `rx_streamer::recv`，理解它的返回值、缓冲区与超时三者关系；
- 逐字段读懂 `rx_metadata_t`，尤其是 `error_code` 各取值的含义，以及"溢出（overflow）"为什么会被"重载"成两种错误；
- 写出一个健壮的接收循环：能区分超时、溢出、坏包三种情况，并配合信号（Ctrl+C）安全停止。

> 本讲承接 u2-l5 建立的概念链：`cpu_format`/`otw_format`、`stream_args_t.channels`、`issue_stream_cmd` 点火、`recv` 返回实际样本数等都不再重复解释。

## 2. 前置知识

在进入源码前，先用一张图建立"数据怎么流动"的直觉。设备端的射频（RF）数据经过数字下变频（DDC）后，被打包成一个个**数据包（packet）**通过传输层（以太网/USB）送到主机；主机侧的 `rx_streamer` 把这些包拆开、做格式转换（如 `sc16` → `fc32`），再填进用户提供的内存缓冲区。这一过程可用下面的数据流表示：

```
设备 RF ─▶ DDC ─▶ [数据包] ─▶ 传输层 ─▶ rx_streamer ─▶ 用户缓冲区 buffs[]
                                    │
                                    └─▶ 每个包还附带一份"元数据" ─▶ rx_metadata_t
```

理解本讲只需要记住三个要点：

1. **包（packet）是基本单位**。`recv` 一次可能填不满你给的缓冲区，也可能一个包大到要分多次填——这就是"分片（fragmentation）"。
2. **每个包都自带一份小档案**，叫元数据：这一批样本是什么时间采的、是不是一帧的开头/结尾、有没有出错。
3. **出错是常态而非异常**。高速采样下，主机读得太慢就会"溢出（overflow）"，丢包会产生"序列错误"。这些不是 C++ 异常，而是通过 `error_code` 字段报告，由你的循环决定怎么办。

> 术语提示：UHD 文档里 **overrun** 和 **overflow** 是同一个东西，都指设备产数据比主机读得快、内部缓冲被撑爆。本讲统一用"溢出"。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `host/include/uhd/types/metadata.hpp` | 定义 `rx_metadata_t` 结构体与 `error_code_t` 枚举，是本讲最核心的数据结构。 |
| `host/include/uhd/stream.hpp` | 声明 `rx_streamer::recv` 与 `issue_stream_cmd`，并用大段注释写明 `recv` 的契约（超时、错误处理、分片、线程安全）。 |
| `host/examples/rx_samples_to_file.cpp` | 真实可编译示例，其 `recv_to_file` 模板函数就是一个完整的健壮接收循环，是本讲的"标准答案"。 |

辅助参考（非必须，用于印证细节）：

| 文件 | 作用 |
| --- | --- |
| `host/lib/types/metadata.cpp` | `rx_metadata_t::strerror()` 的实现，展示每个错误码如何转成可读字符串。 |
| `host/include/uhd/types/stream_cmd.hpp` | `stream_cmd_t` 结构，理解 `issue_stream_cmd` 点火/停止的参数。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应学习目标的递进：先会**调**（4.1 recv），再会**读**（4.2 元数据），最后会**组装成循环**（4.3 接收循环）。

### 4.1 rx_streamer::recv

#### 4.1.1 概念说明

`recv` 是整个接收 API 的**唯一主入口**。它的职责可以用一句话概括：**阻塞地把设备送来的样本填进用户缓冲区，直到某个"返回条件"成立，然后告诉你实际填了多少样本、以及这一帧出了什么状况**。

注意 `recv` 是个**纯虚函数**：

[host/include/uhd/stream.hpp:327-331](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L327-L331) —— `rx_streamer` 只是个抽象基类，真正的接收逻辑由各设备驱动（如 MPMD/RFNoC 设备）以虚函数分派实现。`stream.cpp` 里甚至连函数体都没有（只有空析构函数），这点 u2-l5 已说明。

`recv` 的签名里有五个参数，含义如下：

| 参数 | 类型 | 含义 |
| --- | --- | --- |
| `buffs` | `ref_vector<void*>` | **可写**缓冲区数组，每个元素对应一个通道的内存指针 |
| `nsamps_per_buff` | `size_t` | 每个缓冲区能装多少个**样本**（不是字节） |
| `metadata` | `rx_metadata_t&` | **输出**参数，调用后被填上本帧的元数据 |
| `timeout` | `double` | 每次内部收包的超时秒数（默认 0.1） |
| `one_packet` | `bool` | 为 true 时，处理完一个包就立即返回 |

#### 4.1.2 核心流程

`recv` 的内部行为可以用下面这段伪代码概括（精简自头文件注释）：

```text
recv(buffs, nsamps_per_buff, md, timeout, one_packet):
    md.reset()                      # 先把元数据清成安全默认值
    filled = 0
    while filled < nsamps_per_buff:
        pkt = 从传输层取下一个包(timeout)
        if 超时且没拿到任何包:
            md.error_code = TIMEOUT
            return filled            # 注意：可能返回 0
        if 包是"end_of_burst"末包:
            md.end_of_burst = true
            把 pkt 剩余样本拷进 buffs
            return filled + 拷贝量
        if 包带错误标志(溢出/坏包/序列错/对齐失败):
            md.error_code = 对应错误码
            return filled            # 已收到的有效样本照常返回
        if one_packet 为真:
            把这一个包的样本拷进 buffs
            return 拷贝量            # 保证"一个包一次返回"
        否则:
            尽量拷贝，填满为止
            filled += 拷贝量
            若包还有剩余 -> 置 more_fragments，下次接着填
    return filled
```

这里有三个**最容易踩坑**的点，全部写在了头文件注释里：

1. **返回值 ≤ 请求量**。`recv` 返回的是"实际填进去的样本数"，几乎总是小于等于 `nsamps_per_buff`，出错时甚至可能为 0。因此**绝不能假设** `recv` 一次就填满缓冲区。
   - 见 [host/include/uhd/stream.hpp:220-224](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L220-L224)。

2. **timeout 是"每次内部收包"的超时，不是总超时**。也就是说，单次 `recv` 调用内部可能发起多次收包，每次都等 `timeout` 秒，累加起来可能远大于 `timeout`。
   - 见 [host/include/uhd/stream.hpp:270-290](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L270-L290)。
   - 当 `timeout == 0` 时，`recv` 会"尽快返回"以降低延迟，但可能即便有别的不严重错误也会报成 TIMEOUT，所以**timeout=0 不能用来可靠地探测所有错误**。

3. **溢出不会立即报告**。设备发生溢出时，FIFO 里往往还有一批**有效样本**没送出来；UHD 会先把这批有效样本如数返回，等 FIFO 被掏空、真正丢数据的那一刻，才把 `error_code` 置成 `OVERFLOW`。
   - 见 [host/include/uhd/stream.hpp:249-268](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L249-L268)。
   - 推论：**一次 `recv` 返回 `OVERFLOW` 时，本次返回的样本数 `filled` 可能 > 0 且全部有效**——丢的是这批之后到下次有效样本之间的"缝隙"。

补充两个契约细节：

- **分片（fragmentation）**：如果一个包比你的缓冲区还大，`recv` 会先填满缓冲区，把剩余样本留到下次调用，并置 `md.more_fragments = true`、`md.fragment_offset` 记下偏移。见 [host/include/uhd/stream.hpp:229-238](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L229-L238)。
- **线程安全**：`recv` 不是线程安全的（为了省锁开销）。同一个流器同一时刻只能有一个线程在 `recv`；不同流器可以分别在不同线程里 `recv`。见 [host/include/uhd/stream.hpp:240-247](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L240-L247)。

#### 4.1.3 源码精读

`recv` 的声明与默认参数：

> [host/include/uhd/stream.hpp:327-331](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L327-L331) 定义 `recv` 纯虚函数，`timeout` 默认 0.1 秒、`one_packet` 默认 false。

与之配套的"点火"接口 `issue_stream_cmd`：

> [host/include/uhd/stream.hpp:333-344](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L333-L344) `issue_stream_cmd` 告诉设备"开始/停止往主机送样本"。在 `recv` 之前必须先点火，否则 `recv` 会一直等到超时。

`stream_cmd_t` 控制点火的方式，其 `stream_mode` 有四种取值，理解它们对读懂接收循环里的"开始/停止"至关重要：

> [host/include/uhd/types/stream_cmd.hpp:38-43](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/stream_cmd.hpp#L38-L43) 四种流模式：`START_CONTINUOUS`（持续采）、`STOP_CONTINUOUS`（停持续采）、`NUM_SAMPS_AND_DONE`（采 N 个就停）、`NUM_SAMPS_AND_MORE`（采 N 个并等待下一条命令）。

> 注意：这四种枚举值被故意设成 `'a'`/`'o'`/`'d'`/`'m'` 四个 ASCII 字符，是 VRT 包里"流模式"字段的线上编码，体现了 enum 值 = 协议字段值的设计。

#### 4.1.4 代码实践

**实践目标**：通过"只取元数据"的特殊用法，直观感受 `recv` 的契约。

操作步骤（**源码阅读型**，无需硬件）：

1. 打开 [host/include/uhd/stream.hpp:292-318](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L292-L318)，阅读 `Calling recv() with nsamps_per_buff = 0` 一节。
2. 注意这段示例逻辑：先用正常 `nsamps` 调一次 `recv`；如果"收到的样本数 < 期望数"**且**错误码是 `NONE` 或 `TIMEOUT`（即"明明有问题却没报出来"），就再用 `nsamps_per_buff = 0` 调一次 `recv` 专门把真正的错误码"逼"出来。

需要观察的现象与预期结果：

- 这印证了 4.1.2 第 2 个坑——`timeout=0`/样本不足时，错误可能被"吞"成 `TIMEOUT`，需要二次 `recv` 才能拿到真实错误码。
- 请用一句话写下：为什么注释强调这次二次调用 `timeout > 0`？（答案：零样本调用仍会按 `timeout` 等待数据，且绝不会返回 `TIMEOUT`，所以能安全取出真正的错误码。）

> 运行结果：**待本地验证**（本实践为阅读理解型，不产生可运行输出）。

#### 4.1.5 小练习与答案

**练习 1**：`recv` 的 `timeout` 参数，是"整次 `recv` 调用的总超时"还是"每次内部收包的超时"？

**参考答案**：是**每次内部收包的超时**。单次 `recv` 内部可能发起多次收包，每次都等 `timeout` 秒，因此整次调用最长可能远大于 `timeout`。

**练习 2**：为什么说"`recv` 返回 0 不一定代表出错"？

**参考答案**：`recv` 返回 0 可能是 `TIMEOUT`（上游暂时没数据，例如突发式信号源的自然现象），也可能只是还没填满缓冲区。是否真出错要查 `md.error_code`，而不是单看返回值。

---

### 4.2 rx_metadata_t

#### 4.2.1 概念说明

`rx_metadata_t` 是"每个数据包附带的小档案"。它解决的问题是：**样本本身只是一串数字，但你需要知道这串数字"是什么时候采的、是不是一帧的边界、有没有丢"**。这些信息样本本身装不下，于是 UHD 把它们抽出来放进 `rx_metadata_t`，由 `recv` 在返回前填好。

它本质上是一个**纯数据 POD 结构**（外加两个格式化方法），定义在：

> [host/include/uhd/types/metadata.hpp:22-155](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/metadata.hpp#L22-L155) `rx_metadata_t` 结构体完整定义。

#### 4.2.2 核心流程

`rx_metadata_t` 的字段可以分成三组来记：

**第一组：时间与边界（"这批样本的坐标"）**

| 字段 | 含义 |
| --- | --- |
| `has_time_spec` | 是否带时间戳 |
| `time_spec` | 首个样本的采样时刻（`time_spec_t`，秒＋小数 ticks） |
| `start_of_burst` | 是否一次采集的开头 |
| `end_of_burst` | 是否一次采集的结尾 |

**第二组：分片（"缓冲区装不下一个包时"）**

| 字段 | 含义 |
| --- | --- |
| `more_fragments` | 还有剩余样本没填完（见 4.1.2 分片说明） |
| `fragment_offset` | 本批样本在原始包里的起始样本号 |
| `eov_positions*` | "End Of Vector"位置数组，用于变长向量流（高级用法，本讲不展开） |

**第三组：错误（最重要的部分）**

| 字段 | 含义 |
| --- | --- |
| `error_code` | `error_code_t` 枚举，见下表 |
| `out_of_sequence` | 是否发生了"乱序/丢包"（用来区分两种 OVERFLOW） |

`error_code_t` 各取值是本讲的重中之重：

> [host/include/uhd/types/metadata.hpp:113-136](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/metadata.hpp#L113-L136) 七种错误码及其十六进制取值。

| 错误码 | 值 | 含义 | 典型处理 |
| --- | --- | --- | --- |
| `ERROR_CODE_NONE` | `0x0` | 一切正常 | 继续接收 |
| `ERROR_CODE_TIMEOUT` | `0x1` | 超时没收到包 | 多数情况退出循环 |
| `ERROR_CODE_LATE_COMMAND` | `0x2` | `stream_cmd` 的时间戳已过期 | 重新发命令 |
| `ERROR_CODE_BROKEN_CHAIN` | `0x4` | 设备期待下一条命令却没等到 | 重新发命令 |
| `ERROR_CODE_OVERFLOW` | `0x8` | 缓冲撑爆 **或** 序列错误（见下） | 打印告警并继续 |
| `ERROR_CODE_ALIGNMENT` | `0xc` | 多通道时间对齐失败 | 检查同步配置 |
| `ERROR_CODE_BAD_PACKET` | `0xf` | 包解析失败（坏包） | 按 `--continue` 决定 |

**关键细节：OVERFLOW 是"重载"的错误码。** 注释说得很直白——为了兼容老程序，UHD 没有为"序列错误"单开一个码，而是复用了 `OVERFLOW`，再用 `out_of_sequence` 这个 bool 来区分到底是"溢出"还是"丢包/乱序"：

> [host/include/uhd/types/metadata.hpp:122-131](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/metadata.hpp#L122-L131) 解释 OVERFLOW 同时承载"溢出"与"序列错误"两种语义的历史原因。

两者的共同点是：**在本次 `time_spec` 和下一次成功接收的 `time_spec` 之间存在数据缺失（缝隙）**。`strerror()` 会据此输出不同文字：

> [host/lib/types/metadata.cpp:60-88](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/types/metadata.cpp#L60-L88) `strerror()` 实现：`OVERFLOW` 分支根据 `out_of_sequence` 拼出 `"(Overflow)"` 或 `"(Out of sequence error)"`。

**安全默认值**：结构体的 `reset()` 把所有字段清成"无时间戳、无分片、无错误"的安全初值，`recv` 每次调用前都会先重置：

> [host/include/uhd/types/metadata.hpp:31-44](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/metadata.hpp#L31-L44) `reset()` 把 `error_code` 置 `NONE`、各 bool 置 false、`time_spec` 置 0。

#### 4.2.3 源码精读

把错误码字符串化的两个方法：

> [host/include/uhd/types/metadata.hpp:142-154](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/metadata.hpp#L142-L154) `to_pp_string()`（美化打印）与 `strerror()`（类似 C 的 `strerror`，只描述错误码）。

> [host/lib/types/metadata.cpp:20-50](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/types/metadata.cpp#L20-L50) `to_pp_string()` 实现：compact 模式只打印非默认字段，verbose 模式打印全部字段。示例程序里没直接用，但调试时很有用。

`out_of_sequence` 字段的注释也值得一看，它点明了"丢包 vs 乱序"的语义：

> [host/include/uhd/types/metadata.hpp:138-140](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/metadata.hpp#L138-L140) `out_of_sequence`：传输层丢包或乱序到达时为 true。

#### 4.2.4 代码实践

**实践目标**：用 `strerror()` / `to_pp_string()` 给一段假想的元数据"画像"。

操作步骤（**源码阅读型**）：

1. 对照 [host/lib/types/metadata.cpp:60-88](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/types/metadata.cpp#L60-L88) 的 switch，在纸上推演下面两种 `md` 各自的 `strerror()` 输出：
   - `md.error_code = OVERFLOW; md.out_of_sequence = false;`
   - `md.error_code = OVERFLOW; md.out_of_sequence = true;`
2. 再对照 `to_pp_string()` 的 verbose 分支，写出 `md`（`has_time_spec=true, time_spec=1.5s, start_of_burst=true, error_code=NONE`）的完整打印串。

预期结果：

- 第一种输出 `ERROR_CODE_OVERFLOW (Overflow)`，第二种输出 `ERROR_CODE_OVERFLOW (Out of sequence error)`。
- verbose 打印会包含 `Has timespec: Yes`、`Time of first sample: 1.5`、`Start of burst: Yes`、`Error Code: ERROR_CODE_NONE`、`Out of sequence: No`。

> 运行结果：**待本地验证**（推演型，可自行写 5 行 `main` 实际打印验证）。

#### 4.2.5 小练习与答案

**练习 1**：`ERROR_CODE_OVERFLOW` 同时表示哪两种错误？靠哪个字段区分？

**参考答案**：同时表示"内部缓冲溢出"和"包序列错误（丢包/乱序）"；靠 `out_of_sequence` 区分——`false` 是溢出，`true` 是序列错误。这是为兼容老程序而保留的历史设计。

**练习 2**：连续两次 `recv`，第一次 `time_spec` = 1.000 s 且 `error_code=NONE`，第二次 `time_spec` = 1.005 s 且 `error_code=OVERFLOW`。假设采样率 10 Msps，这两帧之间丢了多少样本？

**参考答案**：两次首样本时间差 0.005 s，按 10×10⁶ sps 应有 \(0.005 \times 10^7 = 50000\) 个样本的时间跨度。但"丢失"的具体样本数无法从元数据精确得知——OVERFLOW 只告诉你"在 1.000 s 到 1.005 s 之间存在缝隙"，缝隙长度需要结合实际收到的样本数与时间戳反推。重点是知道**这段时间的数据不可信、不能拼成连续流**。

---

### 4.3 接收循环

#### 4.3.1 概念说明

真实的接收程序永远不会只调一次 `recv`。你需要一个**循环**：反复 `recv` → 检查元数据 → 处理错误 → 把样本写盘/处理 → 判断是否该停。这个循环是 UHD 所有接收示例的"心脏"，写好它就掌握了 80% 的接收侧工程实践。

`rx_samples_to_file` 把这个循环封装在模板函数 `recv_to_file<T>` 里，是公认的标准写法。本节就逐段拆解它。

#### 4.3.2 核心流程

整个接收循环可以分成 **准备 → 点火 → 循环 → 收尾** 四个阶段：

```text
准备阶段:
    1. 构造 stream_args_t(cpu, otw)，设 channels
    2. rx_stream = usrp->get_rx_stream(stream_args)
    3. 为每个通道分配裸数组 buffs[ch]      # 注意：不能用 std::vector 第二维
    4. 打开每通道输出文件

点火阶段:
    5. 构造 stream_cmd_t(模式由 num_requested_samples 决定)
       - num==0 -> START_CONTINUOUS（持续采）
       - num>0  -> NUM_SAMPS_AND_DONE（采 N 个停）
    6. rx_stream->issue_stream_cmd(stream_cmd)

循环阶段 (while 未停 && 未采够 && 未超时):
    7. num_rx = rx_stream->recv(buffs, spb, md, 3.0, enable_size_map)
    8. switch md.error_code:
         TIMEOUT     -> 打印 "Timeout" 并 break 退出循环
         OVERFLOW    -> 打印一次性告警, continue（丢弃这批元数据，继续）
         其它非 NONE -> md.strerror(); 按 --continue 决定 continue 还是 throw
         NONE        -> 正常: num_total += num_rx; 写盘
    9. 累计样本数 / 带宽统计

收尾阶段:
   10. issue_stream_cmd(STOP_CONTINUOUS)   # 告诉设备停
   11. 关闭文件、delete[] 释放每通道缓冲
```

三个工程要点：

1. **缓冲区必须用裸数组，不能用 `std::vector`**。因为 `recv` 内部会对每个子数组做 `reinterpret_cast<char*>`，这与 `std::vector` 的内存模型不兼容。源码里专门留了注释解释这一点。
2. **超时给的是 3.0 秒**（而非默认 0.1），是示例为了在写盘慢的情况下少报误超时而设的经验值。
3. **`one_packet` 绑定到 `--sizemap`**：开了包大小统计就必须一个包一次返回，否则统计会失真。

#### 4.3.3 源码精读

**准备阶段**——构造流器与缓冲区：

> [host/examples/rx_samples_to_file.cpp:163-165](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L163-L165) 用 `stream_args_t` + `channels` 调 `get_rx_stream` 得到接收流器。

> [host/examples/rx_samples_to_file.cpp:167-182](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L167-L182) 声明 `uhd::rx_metadata_t md;` 并为每个通道 `new samp_type[samps_per_buff]`。注意注释明确解释为何不用 `std::vector`，并且用 `try/catch(std::bad_alloc)` 处理大缓冲分配失败。

**点火阶段**——根据是否限定样本数选择流模式：

> [host/examples/rx_samples_to_file.cpp:206-213](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L206-L213) 设 `stream_mode`、`num_samps`、`stream_now`（单通道立即采，多通道用 `time_spec` 对齐）、`time_spec`（当前时间 +50 ms），然后 `issue_stream_cmd`。

**循环条件**——三重终止条件：

> [host/examples/rx_samples_to_file.cpp:226-228](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L226-L228) `while (not stop_signal_called && (未采够 || 持续模式) && (无时限 || 未超时))`。任一条件不满足即退出。

**核心 recv 调用与三分支错误处理**——这是全讲最关键的一段：

> [host/examples/rx_samples_to_file.cpp:231-232](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L231-L232) `recv(buffs, samps_per_buff, md, 3.0, enable_size_map)`，返回实际样本数。

> [host/examples/rx_samples_to_file.cpp:234-238](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L234-L238) **TIMEOUT 分支**：打印 "Timeout while streaming" 并 `break` 退出循环（视为结束信号）。

> [host/examples/rx_samples_to_file.cpp:239-254](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L239-L254) **OVERFLOW 分支**：用全局 `overflow_message` 标志保证**告警只打印一次**，并算出所需写盘速率 \( \text{rate} \times \text{channels} \times \text{sizeof(samp)} / 10^6 \) MB/s，然后 `continue`（不写这批、继续下一轮）。这里没有累计溢出次数——本讲综合实践就来补这一刀。

> [host/examples/rx_samples_to_file.cpp:255-263](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L255-L263) **其它错误分支**：调 `md.strerror()` 得到可读串，若命令行带 `--continue` 则打印并继续，否则 `throw std::runtime_error` 终止。

**写盘与带宽统计**：

> [host/examples/rx_samples_to_file.cpp:265-291](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L265-L291) 累计 `num_total_samps`、按通道写盘（每样本字节数 = `sizeof(samp_type)`），每秒结算一次瞬时带宽 \( \text{bw} = \Delta\text{samps} / \Delta t \)。

**收尾阶段**——停采与释放：

> [host/examples/rx_samples_to_file.cpp:295-306](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L295-L306) 发 `STOP_CONTINUOUS` 命令停采、关闭文件、`delete[]` 释放每通道缓冲。

**信号处理**——Ctrl+C 如何安全打断持续采集：

> [host/examples/rx_samples_to_file.cpp:43-49](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L43-L49) 全局 `stop_signal_called` 初值 false；`sig_int_handler` 把它置 true。循环条件在每轮迭代顶部检查它，从而在当前 `recv` 返回后（最多再等 3.0 s 超时）优雅退出，而不是被信号硬中断。

#### 4.3.4 代码实践

**实践目标**：改造 OVERFLOW 分支，使其**每次溢出都打印一条带累计次数的告警**（对应本讲的综合实践预热版）。

操作步骤：

1. 在 `recv_to_file` 函数体顶部、`unsigned long long num_total_samps = 0;` 附近，新增一个计数器（示例代码）：
   ```cpp
   // 示例代码：新增溢出计数器
   unsigned long long overflow_count = 0;
   ```
2. 把 OVERFLOW 分支里的 `if (overflow_message) { ... }` 改成"每次都打印并计数"（示例代码）：
   ```cpp
   // 示例代码：替换原 rx_samples_to_file.cpp:239-254 的 OVERFLOW 分支
   if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_OVERFLOW) {
       const std::lock_guard<std::mutex> lock(recv_mutex);
       overflow_count++;
       std::cerr << boost::format("Warning: overflow #%d (seq_err=%s), "
                                  "samples dropped between timestamps\n")
                    % overflow_count
                    % (md.out_of_sequence ? "yes" : "no");
       continue;
   }
   ```
3. 在 `if (stats) { ... }` 统计段里追加一行打印总溢出次数（示例代码）：
   ```cpp
   // 示例代码：在统计输出末尾追加
   std::cout << boost::format("%sTotal overflows: %d")
                   % thread_prefix % overflow_count << std::endl;
   ```

需要观察的现象与预期结果：

- 持续采集且写盘跟不上时，stderr 会**反复**出现 `Warning: overflow #1 ...`、`#2 ...`（原版只出现一次）。
- 程序结束时，stdout 会打印 `Total overflows: N`。
- 注意保留 `continue`——溢出时这批样本的时间戳有缝，**不应写盘**，否则文件里会出现不连续段。

> 运行结果：**待本地验证**（需要真实 USRP 硬件或可触发溢出的高采样率场景；无硬件时可只做代码修改与编译检查）。

> 为什么用 `std::lock_guard<std::mutex> lock(recv_mutex)`？因为 `--multi-streamer` 模式下多个线程各跑一个 `recv_to_file`，它们的告警/统计输出会交错，需要这把全局互斥锁串行化 `std::cerr`/`std::cout`。`recv` 本身虽然非线程安全，但每个线程用的是**各自独立的** `rx_stream`，所以多线程 `recv` 不冲突。

#### 4.3.5 小练习与答案

**练习 1**：为什么 OVERFLOW 分支用 `continue` 而不是 `break`？

**参考答案**：因为溢出（在持续采集模式下）是**可恢复**的——设备会自动复位 FIFO 并继续送样本，丢的只是 `time_spec` 缝隙里的数据。`break` 会导致一次溢出就结束整个采集，而正确做法是丢弃这批有缝的元数据、继续接收后续有效样本。注意：仅在**持续模式**下如此；其它模式溢出后会停流，需要重新 `issue_stream_cmd`。

**练习 2**：循环条件为什么把 `stop_signal_called` 放在 `while` 顶部，而不是在 `recv` 之后再检查？

**参考答案**：`recv` 是阻塞调用（最长可阻塞数倍 `timeout`）。把信号检查放在 `while` 顶部，能保证在当前 `recv` 返回后的下一轮迭代立即退出；若放在 `recv` 之后才检查，逻辑等价但可读性更差。关键是**无法在 `recv` 阻塞期间中断它**——只能等它超时返回，所以 `timeout`（示例里 3.0 s）也间接决定了 Ctrl+C 的响应延迟。

**练习 3**：示例里 `recv` 的 `timeout` 为什么用 3.0 而不是默认 0.1？

**参考答案**：0.1 s 在写盘较慢或系统繁忙时极易触发误 `TIMEOUT` 进而提前 `break`。3.0 s 给了足够的容错窗口，减少"其实只是暂时没数据"被误判成"流真的结束"的概率。但它也意味着 Ctrl+C 最坏要等约 3 s 才生效（见练习 2）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个"**带溢出统计的健壮接收循环**"小任务。

**任务**：在 `recv_to_file` 中实现一个更完整的错误报告循环，要求：

1. 区分**三种**溢出语义：纯溢出（`out_of_sequence=false`）、序列错误（`true`）、以及多通道对齐失败（`ALIGNMENT`，单独计数）；
2. 每种错误分别累计次数，并在 `stats` 段统一打印；
3. 维持原有的"TIMEOUT 退出、坏包按 `--continue` 决定"语义不变。

**参考实现思路**（示例代码骨架）：

```cpp
// 示例代码：放在 num_total_samps 旁边
unsigned long long overflow_count = 0, seqerr_count = 0, align_count = 0;

// ... 循环内 ...
switch (md.error_code) {
case uhd::rx_metadata_t::ERROR_CODE_TIMEOUT:
    std::cout << thread_prefix << "Timeout while streaming\n";
    goto done;                       // 跳出循环
case uhd::rx_metadata_t::ERROR_CODE_OVERFLOW:
    if (md.out_of_sequence) seqerr_count++; else overflow_count++;
    continue;
case uhd::rx_metadata_t::ERROR_CODE_ALIGNMENT:
    align_count++;
    continue;                        // 对齐失败也尝试继续
default:                             // 含 BAD_PACKET/LATE_COMMAND/...
    // 交给原有 "其它错误" 分支处理
    ;
}
// ... stats 段 ...
std::cout << boost::format("%sOverflows=%d SeqErr=%d AlignFail=%d\n")
                % thread_prefix % overflow_count % seqerr_count % align_count;
```

**验收标准**：

- 编译通过（`cd host/build && cmake .. && make rx_samples_to_file`，**待本地验证**）；
- 在持续采集、故意压低写盘速率的情况下，stderr/stdout 能分别报告三类错误次数；
- 文件中不包含时间戳有缝的样本段（溢出/序列错误时 `continue` 不写盘）。

> 无硬件时：把本任务降级为"代码评审型"——只完成修改与编译，逐行说明每个 `case` 对应 `error_code_t` 的哪个枚举值，并解释为何对齐失败也可尝试 `continue`。

## 6. 本讲小结

- `rx_streamer::recv` 是接收唯一主入口，返回**实际样本数**（≤ 请求量，出错时可为 0）；`timeout` 是**每次内部收包**的超时而非总超时；它非线程安全，但每个独立流器可分线程并用。
- 溢出（overrun/overflow）**不会立即报告**：UHD 会先把 FIFO 里残留的有效样本如数返回，掏空那一刻才置 `error_code=OVERFLOW`，所以一次返回 OVERFLOW 时本次样本可能仍有效。
- `rx_metadata_t` 分三组字段：时间与边界（`has_time_spec`/`time_spec`/`start_of_burst`/`end_of_burst`）、分片（`more_fragments`/`fragment_offset`）、错误（`error_code` + `out_of_sequence`）。
- `ERROR_CODE_OVERFLOW` 被**重载**为"溢出"与"序列错误"两种语义，靠 `out_of_sequence` 区分，这是为兼容老程序保留的历史设计；`strerror()` 据此输出不同文字。
- 标准接收循环 = 准备（裸数组缓冲 + 多通道文件）→ 点火（`issue_stream_cmd`，`num==0` 用 `START_CONTINUOUS`）→ 循环（TIMEOUT 退、OVERFLOW 续、坏包按 `--continue`）→ 收尾（`STOP_CONTINUOUS` + 释放）。
- 信号安全停止靠全局 `stop_signal_called` + `while` 顶部检查；`recv` 阻塞期间无法中断，故 `timeout` 也决定了 Ctrl+C 的响应延迟。

## 7. 下一步学习建议

- **下一讲 u2-l7（发送流与波形生成）** 会讲对称的发送侧：`tx_streamer::send`、`tx_metadata_t` 的 `has_time_spec`/`start_of_burst`/`end_of_burst`，以及欠流（underflow）这种**异步事件**如何用 `recv_async_msg` 读取——你会发现它和本讲的 `rx_metadata_t` 是镜像关系。
- 若想深究 `recv` 在 RFNoC 设备上到底怎么把包拆开、怎么触发 OVERFLOW，可在学完第三单元（RFNoC）后回到 `host/lib/rfnoc/` 下找流器实现（本手册 u3 系列与 u4-l3 VRT 包协议会覆盖）。
- 想立刻动手但又没硬件的读者，可先看 `host/tests/` 下与流/缓冲相关的测试（本手册 u5-l5 会讲测试体系），用测试断言反推 `recv` 的边界行为。
