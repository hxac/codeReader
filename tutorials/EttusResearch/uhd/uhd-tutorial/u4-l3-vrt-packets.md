# VRT 包协议与 super packet handler

## 1. 本讲目标

上一讲（u4-l2）我们钻进了传输层，看到主机样本缓冲如何被「帧池 + 侵入式引用计数」搬过物理链路。但传输层只负责搬运一帧帧原始字节，它并不知道这一帧里：

- 前几个字是头部、后几个字是样本？
- 这个包属于哪个流（stream）？
- 它带不带时间戳？是 burst 的开头还是结尾？
- 它到底是一个装样本的「数据包」，还是一个报告溢出/欠流的「控制包」？

回答这些问题是本讲的任务。学完本讲你应该能够：

1. 说清 VRT（VITA Radio Transport）/ CHDR 包头部的字段布局，以及 pack/unpack 的契约。
2. 理解 `super_recv_packet_handler` 如何在快路径上批量收包、做多通道时间对齐、并把控制包就地转成 `error_code`。
3. 理解 `super_send_packet_handler` 如何把用户一次 `send()` 拆分成若干个带 VRT 头的线缆包、并处理分片与 burst 边界。
4. 区分「数据包」与「控制包（context / inline message）」两种语义，以及它们如何复用同一套头部。

## 2. 前置知识

- **VRT / CHDR**：USRP 线缆上的样本封装格式。老设备（USRP1/2/B100 等）用 VRT/VRIP 链路层，现代设备（B200/X 系列、RFNoC）用 CHDR（CHDR = CHDR Header，本质是 VRT 头的精简变体）。本讲会看到 UHD 用同一套 pack/unpack 代码同时服务两者，靠 `link_type` 字段切换。
- **字（word32）**：VRT/CHDR 的长度单位是 32 位字。一个包由「头部若干字 + 负载若干字 + 可选尾部 1 字」组成。
- **大端/小端（be/le）**：线缆字节序。UHD 提供 `if_hdr_pack_be`/`if_hdr_pack_le` 两组函数，主机字节序由 `byteswap` 宏抹平。
- **快路径（fast path）**：每来一个样本包都要执行的代码。这里任何一次多余分支或内存分配都会被采样率放大，所以 UHD 不惜用代码生成器把解析逻辑展开成跳转表。
- **flow control（流控）**：设备端用包序号告诉主机「我发到第几个包了」，主机据此回馈。本讲会看到流控 ACK 包如何被夹在数据包流里被静默消费。
- 建议先读完 u2-l5（stream_args/收发流器）、u2-l6（rx_metadata_t）、u4-l1（convert 子系统）、u4-l2（传输层），本讲是它们脚下那层「包协议」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `host/include/uhd/transport/vrt_if_packet.hpp` | 公共头：定义包元数据结构 `if_packet_info_t` 与 pack/unpack 四个 API 及其契约。 |
| `host/lib/transport/gen_vrt_if_packet.py` | Mako 代码生成器：把 128 种头部组合展开成跳转表，生成真正的 pack/unpack C++ 实现。 |
| `host/lib/transport/super_recv_packet_handler.hpp` | 接收侧批量包处理器（`sph` 命名空间）：收包、解头、对齐、流控、把控制包转成 `error_code`、拷贝转换。 |
| `host/lib/transport/super_send_packet_handler.hpp` | 发送侧批量包处理器：把元数据打包成 VRT 头、分片、转换、提交（release 触发真正发包）。 |

辅助理解（非精读对象，用于印证接线）：`host/lib/usrp/b200/b200_io_impl.cpp`（CHDR 链路层接线示例）、`host/tests/vrt_test.cpp`（头部字段往返测试）、`host/tests/sph_recv_test.cpp`（接收处理器单测）。

## 4. 核心概念与源码讲解

### 4.1 vrt_if_packet：VRT/CHDR 包格式与解析

#### 4.1.1 概念说明

`vrt_if_packet` 是「线缆字节 ↔ C++ 结构体」的翻译层。它解决一个核心问题：线缆上每一个样本包都自带一段自描述头部（这个包多长、属于哪个流、带不带时间戳、是不是 burst 边界），而主机代码更愿意操作一个强类型的 C++ 结构。`if_hdr_pack_*` 把结构压成头部字节，`if_hdr_unpack_*` 把头部字节解回结构。

它有两层不变量值得记住：

1. **同一个结构服务三种链路层**：`LINK_TYPE_NONE`（裸 VRT 头）、`LINK_TYPE_CHDR`（精简头，现代设备）、`LINK_TYPE_VRLP`（带 VRLP 帧的老式链路）。切换只发生在第一个字。
2. **同一个函数服务两种字节序**：`be`/`le` 后缀只是换了 `byteswap` 宏，对调用方完全透明。

#### 4.1.2 核心流程

包的内存布局（以最常用的 `LINK_TYPE_CHDR` / `LINK_TYPE_NONE` 为例）：

```text
┌─────────────┬──────┬──────┬───────┬────────┬──────────────┬───────┐
│ header word │ SID? │ CID? │ TSI?  │ TSF?   │   payload    │ Tlr?  │
│ (1 word)    │ 0..1 │ 0..2 │ 0..1  │ 0..2   │ (样本字节)    │ 0..1  │
└─────────────┴──────┴──────┴───────┴────────┴──────────────┴───────┘
   word 0      可选字段（由头部里的 has_* 位决定是否存在）
```

头部第一个 32 位字（`vrt_hdr_word32`）编码了「后面跟着哪些可选字段」以及「整个包多长」。UHD 在代码生成器里把这些 bit 位硬编码成下面的布局（见 4.1.3 源码精读）：

| 比特位 | 字段 | 含义 |
|---|---|---|
| 31–29 | `packet_type` | 包类型（3 位），区分数据/控制/命令等 |
| 28 | SID present | 是否带流 ID（Stream ID） |
| 27 | CID present | 是否带类 ID（Class ID，UHD 未真正实现值） |
| 26 | Trailer present | 是否带尾部字 |
| 25 | SOB | start-of-burst，burst 起点 |
| 24 | EOB | end-of-burst，burst 终点 |
| 23–22 | TSI size | 整数时间戳大小指示（UHD 固定填 `11`，表示带 32 位整数时间） |
| 21–20 | TSF size | 小数时间戳大小指示（UHD 固定填 `01`，表示带 64 位小数时间） |
| 19–16 | `packet_count` | 包序号（VRLP/NONE 链路层只有 4 位） |
| 15–0 | `num_packet_words32` | 整包长度，单位 32 位字 |

> 说明：CHDR 链路层会把这个 VRT 头重映射成 CHDR 头——CHDR 头的 bits 15–0 是**字节数**而非字数，bits 27–16 是 12 位包序号，bit 31 是 context（控制）包标志。映射在 `chdr_to_vrt` / `vrt_to_chdr` 里完成。

解析与打包的契约（pack/unpack contract）是对称的：

- **pack**：调用方必须把所有 `has_*` 置好、且对每个 `has_*=true` 的字段填好值，并提供 `num_payload_bytes`/`num_payload_words32`；函数回填 `num_header_words32` 与 `num_packet_words32`，并把头部写入 `packet_buff`。
- **unpack**：调用方必须提供 `num_packet_words32`（即「这个包一共几个字可读」）和 `link_type`；函数回填 `packet_type`、`num_payload_*`、`num_header_words32`、所有 `has_*` 及其值、`sob`/`eob` 等；头部非法时抛 `uhd::value_error`。

#### 4.1.3 源码精读

**元数据结构 `if_packet_info_t`** 是整个子系统的核心数据结构。注意它把「必填/派生」直接写进注释：

- [host/include/uhd/transport/vrt_if_packet.hpp:27-86](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/vrt_if_packet.hpp#L27-L86)：`if_packet_info_t` 结构。其中 `packet_type` 枚举同时容纳「VRT 语言」（DATA/IF_EXT/CONTEXT）与「CHDR 语言」（FC/ACK/CMD/RESP/ERROR），两套语义复用同一个 3 位字段——这正是「数据包 vs 控制包」复用同一头部的根源。
- [host/include/uhd/transport/vrt_if_packet.hpp:17-20](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/vrt_if_packet.hpp#L17-L20)：两个关键常量。`num_vrl_words32 = 3` 是 VRLP 链路层额外占用的字数（VRLP 魔数 + 长度字 + VEND 尾字）；`max_if_hdr_words32 = 7` 是头部最大字数，注释点明其构成「hdr+sid+cid+tsi+tsf = 1+1+2+1+2 = 7」。
- [host/include/uhd/transport/vrt_if_packet.hpp:88-149](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/vrt_if_packet.hpp#L88-L149)：pack/unpack 契约的权威文档。unpack 的「要求」里明确：`packet_buff[0]` 必须总是合法的第一个头字，因此 `num_packet_words32` 至少为 1。

**真正的 pack/unpack 实现由生成器产出**，所以精读对象是 `gen_vrt_if_packet.py`：

- [host/lib/transport/gen_vrt_if_packet.py:9-16](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/gen_vrt_if_packet.py#L9-L16)：生成器文档字符串，点明「生成跳转表以加速解析」这一设计意图。
- [host/lib/transport/gen_vrt_if_packet.py:160-165](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/gen_vrt_if_packet.py#L160-L165)：组装最终的 `vrt_hdr_word32`——这四行就是上表布局的源头：

  ```cpp
  vrt_hdr_word32 = uint32_t(0
      | (if_packet_info.packet_type << 29)        // bits 31-29
      | vrt_hdr_flags                              // bits 20-28 的存在性位
      | ((if_packet_info.packet_count & 0xf) << 16) // bits 19-16
      | (if_packet_info.num_packet_words32 & 0xffff) // bits 15-0
  );
  ```

- [host/lib/transport/gen_vrt_if_packet.py:122-147](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/gen_vrt_if_packet.py#L122-L147)：`vrt_hdr_flags` 各 bit 的置位——`SID: 0x1<<28`、`CID: 0x1<<27`、`TSI: 0x3<<22`、`TSF: 0x1<<20`、`Trailer: 0x1<<26`、`EOB: 0x1<<24`、`SOB: 0x1<<25`。
- [host/lib/transport/gen_vrt_if_packet.py:119-156](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/gen_vrt_if_packet.py#L119-L156)：**跳转表优化的核心**——`% for pred in range(2**7)` 把 7 个可选存在位（sid/cid/tsi/tsf/tlr/sob/eob）的 \(2^7=128 \) 种组合在编译期全部展开成 `switch` 的 `case`。运行时无需循环判断「这个字段在不在」，直接跳到对应分支顺序读写。这正是「快路径」的关键。
- [host/lib/transport/gen_vrt_if_packet.py:44-62](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/gen_vrt_if_packet.py#L44-L62)：unpack 侧的运行时存在位预测表。`pred_table_index(hdr) = (hdr >> 20) & 0x1ff` 取头部的 bits 28–20（9 位），查 `pred_unpack_table` 得到 7 位 `pred` 掩码，再喂给上面那个 128 路 switch。
- [host/lib/transport/gen_vrt_if_packet.py:71-94](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/gen_vrt_if_packet.py#L71-L94)：CHDR ↔ VRT 头映射 `chdr_to_vrt` / `vrt_to_chdr`。注意 `vrt = (bytes + 3) / 4`，即字数 = 字节数向上取整：

  \[
  \text{words32} = \left\lceil \frac{\text{bytes}}{4} \right\rceil = \left\lfloor \frac{\text{bytes}+3}{4} \right\rfloor
  \]

- [host/lib/transport/gen_vrt_if_packet.py:262-295](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/gen_vrt_if_packet.py#L262-L295)：三种链路层的 pack 分派。`LINK_TYPE_VRLP` 在前面加 `'VRLP'` 魔数字与长度字、末尾加 `'VEND'`，并把 `num_header_words32 += 2`、`num_packet_words32 += 3`，与常量 `num_vrl_words32` 吻合。
- [host/lib/transport/gen_vrt_if_packet.py:342-343](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/gen_vrt_if_packet.py#L342-L343)：生成器入口对 `be`/`le` 各渲染一遍，于是产出的 `.cpp` 里同时有 `if_hdr_pack_be/_le` 四个符号。

#### 4.1.4 代码实践

**实践目标**：亲手确认 VRT 头部的字段布局，验证 pack→unpack 往返无损。

**操作步骤**：

1. 打开 `host/tests/vrt_test.cpp`，阅读 `pack_and_unpack()` 辅助函数（[host/tests/vrt_test.cpp:17-67](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/vrt_test.cpp#L17-L67)）。它先把 `if_packet_info_in` pack 进 `packet_buff`，打印前 5 个字（注意它对每个字调了 `uhd::byteswap`，是为了在小端主机上以「大端线缆序」打印），再 unpack 回 `if_packet_info_out` 并逐字段断言相等。
2. 关注 `test_with_chdr`（[host/tests/vrt_test.cpp:165-179](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/vrt_test.cpp#L165-L179)）：它设 `link_type = LINK_TYPE_CHDR`、`has_tsf = true`、`has_tlr = false`（注释明说「tlr not supported in CHDR」）。
3. 若已按 u1-l3 构建 host，可运行：

   ```bash
   cd host/build
   ctest -R vrt_test --output-on-failure
   ```

**需要观察的现象**：测试若通过，说明 128 种头部组合中抽样的几种 pack/unpack 往返完全一致；打印的 `packet_buff[0]` 最高字节（bits 31–24）会同时编码 `packet_type` 与各存在位。

**预期结果**：所有 `BOOST_CHECK_EQUAL` 通过。**若本地无构建环境，标注「待本地验证」**，可改为静态阅读：对照本讲 4.1.2 的位布局表，手算 `test_with_sid`（packet_count=1, has_sid, payload=11 字）情形下 `vrt_hdr_word32` 的值，再与生成器逻辑核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `max_if_hdr_words32` 恰好等于 7？请列出它的构成。

**参考答案**：1（header word）+ 1（SID，32 位）+ 2（CID，64 位）+ 1（TSI，32 位）+ 2（TSF，64 位）= 7。Trailer 不计入「头部」字数，它是包尾。

**练习 2**：在 CHDR 链路层，`packet_count` 是几位？在 VRLP/NONE 链路层又是几位？这从哪里能看出来？

**参考答案**：CHDR 是 12 位（`chdr_to_vrt` 里 `(chdr >> 16) & 0xfff`）；VRLP/NONE 是 4 位（pack 时 `(packet_count & 0xf) << 16`）。接收侧 `super_recv_packet_handler` 据此选序列号掩码：`LINK_TYPE_NONE ? 0xf : 0xfff`。

### 4.2 super_recv_packet_handler：批量接收与多通道对齐

#### 4.2.1 概念说明

传输层一次只给你「一个通道的一个帧」。但用户的 `rx_streamer::recv()` 想要的是「N 个通道、凑够 K 个样本、塞进我的缓冲」。`super_recv_packet_handler`（`sph` 命名空间）就是填平这层鸿沟的「批量包处理器」：它代表一组共享同一采样率的通道，在 `recv()` 里把它们齐刷刷地收齐、对齐、拷贝转换。

它额外承担三件传输层不管的事：

1. **解 VRT 头**：把每帧的头部解成 `if_packet_info_t`，提取时间戳、burst 标志、包序号。
2. **多通道对齐**：N 个通道的包必须按时间戳（`tsf`）对齐后才能一起拷给用户——否则左右声道会错位。
3. **控制包分流**：设备会在数据流里夹带「context / inline message」包来报告溢出、迟到命令等。处理器要把它们拦截下来、转成 `rx_metadata_t::error_code`，绝不能让它们被当成样本拷给用户。

#### 4.2.2 核心流程

`recv()` 的整体调用链（[host/lib/transport/super_recv_packet_handler.hpp:225-277](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L225-L277)）：

```text
recv(buffs, nsamps, metadata, timeout, one_packet)
  │
  ├─ 若上一轮 queue 了 error → 回放 metadata，返回 0（除非是 TIMEOUT）
  ├─ recv_one_packet(...)              # 拿「一批对齐好的样本」
  │     ├─ data_bytes_to_copy==0 ? get_aligned_buffs(timeout)  # 需要新包
  │     │     └─ 对每个待对齐通道 index：
  │     │           get_and_process_single_packet(index, ...)  # 收+解头+判类型
  │     │           alignment_check(index, info)               # 按时间戳对齐
  │     ├─ metadata = info.metadata
  │     ├─ 计算 nsamps_to_copy = min(用户要的, 包里有的)
  │     └─ convert_to_out_buff(i)  对每通道：_converter->conv + 推进指针 + 释放帧
  ├─ one_packet 或 end_of_burst → 直接返回
  └─ 否则循环 recv_one_packet 直到填满 buffs 或遇到 error
```

`get_and_process_single_packet` 是每个通道收一个包的核心，返回值是 5 种 `packet_type` 之一，判定顺序在源码里被标注为「THE ORDER IS HOLY」（顺序神圣不可调换）：

```text
1) packet_type != DATA          → PACKET_INLINE_MESSAGE   (控制/上下文包)
2) 包序号 != 期望序号            → PACKET_SEQUENCE_ERROR   (丢包/乱序)
3) has_tsf 且 时间戳倒退         → PACKET_TIMESTAMP_ERROR (时间倒退)
4) 否则                         → PACKET_IF_DATA          (正常样本)
```

多通道对齐的直觉：把所有通道当前包的时间戳（`tsf`）里**最大**的那个当作「对齐锚点」（[host/lib/transport/super_recv_packet_handler.hpp:528-543](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L528-L543)），早于锚点的包被丢弃重收，直到所有通道都拿到同一时间戳的包。`start_of_burst` 跨通道取**与**（全部有才算 burst 起点），`end_of_burst` 取**或**（任一通道 EOB 即结束）。

#### 4.2.3 源码精读

- [host/lib/transport/super_recv_packet_handler.hpp:28-48](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L28-L48)：`sph` 命名空间与类注释——「一个接收处理器代表一组通道，共享采样率，在 `recv()` 里齐声接收」。
- [host/lib/transport/super_recv_packet_handler.hpp:52-56](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L52-L56)：关键回调与函数指针类型。`vrt_unpacker_type` 是裸函数指针 `void(*)(const uint32_t*, if_packet_info_t&)`，正是上节 `if_hdr_unpack_be/_le` 的签名——设备在接线时把对应字节序/链路层的解包函数塞进来。
- [host/lib/transport/super_recv_packet_handler.hpp:393-489](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L393-L489)：`get_and_process_single_packet` 全貌。要点：
  - [L402-404](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L402-L404)：`get_buff(timeout)` 取帧，`nullptr` → `PACKET_TIMEOUT_ERROR`。
  - [L423-429](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L423-L429)：调 `_vrt_unpacker` 解头，`info.time = ifpi.tsf`，`copy_buff` 指向头部之后的负载起点。
  - [L432-436](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L432-L436)：流控——每 `fc_update_window` 个包回馈一次。
  - [L439-448](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L439-L448)：**流控 ACK 包夹在数据流里被静默消费**——若 `ifpi.fc_ack`，调 ACK 回调后 `continue` 再取下一帧，对用户完全不可见。
  - [L459-485](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L459-L485)：上面「THE ORDER IS HOLY」四步判定。
- [host/lib/transport/super_recv_packet_handler.hpp:30-37](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L30-L37)：`get_context_code`——从控制包负载的首字取一个字节，映射成 `error_code`。这就是 4.4 节要讲的「控制包 → error_code」桥梁。
- [host/lib/transport/super_recv_packet_handler.hpp:611-680](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L611-L680)：`get_aligned_buffs` 里对 5 种 `packet_type` 的分发。其中 [L629-656](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L629-L656) 处理 `PACKET_INLINE_MESSAGE`：经 `get_context_code` 得到 `error_code`，若是 `OVERFLOW` 则调 `handle_overflow()` 并打快速日志 `"O"`；[L666-679](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L666-L679) 处理 `PACKET_SEQUENCE_ERROR`：置 `out_of_sequence=true`、`error_code=OVERFLOW`、打日志 `"D"`——这与 u2-l6 讲的「OVERFLOW 被重载为溢出与序列错误两种语义」完全对上。
- [host/lib/transport/super_recv_packet_handler.hpp:175-182](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L175-L182)：`set_converter` 在装好转换器后立刻 `set_scale_factor(1 / 32767.)`——接收侧把 sc16 的整数样本缩放回 \([-1,1]\) 浮点，与发送侧的 `32767.` 互逆（见 u4-l1）。
- [host/lib/transport/super_recv_packet_handler.hpp:802-844](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L802-L844)：`recv_packet_streamer` 同时继承 `recv_packet_handler` 与 `rx_streamer`，把抽象流器接口委托给处理器——这就是 u2-l5 留的「recv 的真正实现由各设备驱动以虚函数分派」在非 RFNoC 设备上的具体落点。

#### 4.2.4 代码实践

**实践目标**：通过单测理解对齐与错误分流的判定顺序。

**操作步骤**：

1. 打开 `host/tests/sph_recv_test.cpp`，定位 `set_vrt_unpacker(&vrt::if_hdr_unpack_be)` 的接线处（如 [host/tests/sph_recv_test.cpp:79](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/sph_recv_test.cpp#L79)）。注意它用 `mock_zero_copy(LINK_TYPE_VRLP)` 伪造一个会按指定 metadata 吐包的传输层。
2. 在测试里搜索注入「错误包」「序列错误包」的用例，观察它们如何驱动 handler 返回特定的 `error_code` 与 `out_of_sequence` 标志。
3. 若已构建，运行：

   ```bash
   cd host/build
   ctest -R sph_recv_test --output-on-failure
   ```

**需要观察的现象**：当 mock 传输层故意丢一个包序号时，下一次 `recv` 返回的 `metadata.error_code == ERROR_CODE_OVERFLOW` 且 `out_of_sequence == true`，这正是 `PACKET_SEQUENCE_ERROR` 分支的效果。

**预期结果**：所有用例通过。**待本地验证**（若无构建环境）可改为：阅读 `get_and_process_single_packet` 的四步判定，解释「为什么必须先判 inline message、再判 sequence」——若顺序反过来，一个夹在流里的溢出通告包会被当作丢包处理，错误归类就错了。

#### 4.2.5 小练习与答案

**练习 1**：流控 ACK 包（`fc_ack`）会出现在用户的 `recv()` 返回值里吗？为什么？

**参考答案**：不会。`get_and_process_single_packet` 在 `ifpi.fc_ack` 为真时调 ACK 回调后 `continue`，直接去取下一帧，ACK 包既不计入样本数也不进入 metadata，对用户完全透明。

**练习 2**：为什么 `start_of_burst` 用「与」、`end_of_burst` 用「或」？

**参考答案**：burst 的起点必须所有通道同时开始才算真正对齐的起点（任一通道没就绪都不算 SOB），故取与；而只要任一通道宣告 EOB，该通道就不再有数据，整个接收就该结束，故取或。见 [L551-554](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L551-L554)。

### 4.3 super_send_packet_handler：批量发送与分片

#### 4.3.1 概念说明

发送侧是接收侧的镜像：用户一次 `tx_streamer::send()` 丢过来「N 个通道、若干样本、带一份 metadata」，处理器要把它切成一个个不超过设备单包上限（`_max_samples_per_packet`）的线缆包，给每个包打上 VRT 头、转换样本格式、提交（release 触发真正发包）。

它要额外处理两个发送侧独有的细节：

1. **分片**：用户给的样本数可能超过单包上限，需要拆成「若干满包 + 一个尾包」。每个分片都要重算时间戳（按采样率累加）。
2. **metadata 缓存**：硬件历史上不支持「0 样本的包」。当用户用 `start_of_burst=true, nsamps=0` 来「预定一个 burst 起点」时，处理器先把这份 metadata 缓存，等下一次真正带样本的 `send()` 时再把它贴到首包上。

#### 4.3.2 核心流程

`send()` 的分派逻辑（[host/lib/transport/super_send_packet_handler.hpp:176-272](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_send_packet_handler.hpp#L176-L272)）：

```text
send(buffs, nsamps, metadata, timeout)
  │
  ├─ 把 tx_metadata_t 翻译成 if_packet_info_t
  │     packet_type=DATA, has_tsf=metadata.has_time_spec,
  │     tsf=metadata.time_spec.to_ticks(tick_rate),
  │     sob=metadata.start_of_burst, eob=metadata.end_of_burst
  │
  ├─ 若有缓存的 metadata 且本次 nsamps>0 → 把缓存的 sob/eob/tsf 贴上来
  │
  ├─ nsamps <= _max_samples_per_packet ?
  │     yes → send_one_packet(buffs, nsamps, ...)
  │            （nsamps==0 且非 SOB → 发一个 1 样本的零包以表达 EOB）
  │     no  → 分片：
  │            num_fragments = (nsamps-1)/max
  │            循环发满包（中间包 sob=false,eob=false, tsf 累加）
  │            最后发尾包（eob=metadata.end_of_burst）
  └─ 返回累计已发样本数
```

`send_one_packet`（[L304-338](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_send_packet_handler.hpp#L304-L338)）做三件事：算负载字节数→每通道取一个发送帧（取不到即超时返回 0）→`convert_to_in_buff` 转换并提交。

`convert_to_in_buff`（[L346-380](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_send_packet_handler.hpp#L346-L380)）的顺序值得记住：**先 pack VRT 头、再转换样本、最后 commit**。`buff->commit(num_vita_words32 * 4)` 触发 u4-l2 讲的「release 即真正发包」。

#### 4.3.3 源码精读

- [host/lib/transport/super_send_packet_handler.hpp:36](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_send_packet_handler.hpp#L36)：类定位注释，与接收侧对称。
- [host/lib/transport/super_send_packet_handler.hpp:182-192](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_send_packet_handler.hpp#L182-L192)：把 `tx_metadata_t` 翻译成 `if_packet_info_t`。注意 `has_tsi=false`（发送侧只带 64 位小数时间 `tsf`），`has_cid=false`，`fc_ack=false`（这是数据包，不是流控 ACK）。
- [host/lib/transport/super_send_packet_handler.hpp:194-208](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_send_packet_handler.hpp#L194-L208)：metadata 缓存逻辑。注释点明：收到「SOB + 0 样本」时缓存，等下次带样本的 send 再贴上；若新 metadata 自带 `time_spec` 则不覆盖时间。
- [host/lib/transport/super_send_packet_handler.hpp:240-271](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_send_packet_handler.hpp#L240-L271)：分片循环。`num_fragments = (nsamps_per_buff - 1) / _max_samples_per_packet`，`final_length = ((nsamps-1) % max) + 1`；每个分片的时间戳按采样率累加（`time_spec + from_ticks(total_sent, _samp_rate)`），中间分片 `sob=false`，尾片 `eob=metadata.end_of_burst`。
- [host/lib/transport/super_send_packet_handler.hpp:131-138](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_send_packet_handler.hpp#L131-L138)：`set_converter` 装好转换器后 `set_scale_factor(32767.)`——把浮点样本放大成 sc16 整数，与接收侧的 `1/32767.` 互逆。
- [host/lib/transport/super_send_packet_handler.hpp:361-375](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_send_packet_handler.hpp#L361-L375)：`convert_to_in_buff` 的 pack→convert→commit 三段式。`otw_mem = buff->cast<uint32_t*>() + _header_offset_words32` 跳过设备私有偏移，`_vrt_packer(otw_mem, if_packet_info)` 打头，转换器紧接着写样本，最后 `commit`。

#### 4.3.4 代码实践

**实践目标**：跟踪发送侧「翻译 metadata → 分片 → 打头 → 转换 → 提交」的完整链路。

**操作步骤**：

1. 打开 `host/tests/sph_send_test.cpp`，阅读它如何构造 `send_packet_handler` 并 `set_vrt_packer(&vrt::if_hdr_pack_be)`（[host/tests/sph_send_test.cpp:40](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/sph_send_test.cpp#L40)）。
2. 选一个用例，在脑中走一遍：用户调用 `send(buffs, nsamps=很大, metadata{start_of_burst=true}, timeout)`。
3. 回答：这次 `send` 会产生几个线缆包？第一个包的 `if_packet_info.sob` 是什么？最后一个包的 `eob` 是什么？中间包的时间戳如何递推？

**需要观察的现象**：当 `nsamps` 远大于 `_max_samples_per_packet` 时，会产生多个分片包；除首包 `sob=true`、尾包 `eob=true` 外，中间包的 `sob=eob=false`，且每个包的 `tsf` 比 previous 多 `max_samples_per_packet / samp_rate` 秒对应的 tick 数。

**预期结果**：能用本节公式手算出分片数与各包时间戳。**待本地验证**：若运行 `ctest -R sph_send_test`，断言会校验返回的已发样本数等于输入样本数。

#### 4.3.5 小练习与答案

**练习 1**：用户调用 `send(buffs, 0, {start_of_burst=true}, t)` 会立即发包吗？

**参考答案**：不会。进入 [L218-222](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_send_packet_handler.hpp#L218-L222) 分支：因为是 SOB 且 nsamps==0，metadata 被缓存（`_cached_metadata=true`），立即返回 0，等下一次带样本的 send 再贴上。

**练习 2**：为什么 `convert_to_in_buff` 里必须「先 pack 头、再 convert 样本」？

**参考答案**：pack 头会回填 `if_packet_info.num_header_words32`，转换器需要据此算出样本应写在头之后的哪个偏移（`otw_mem += num_header_words32`）。若先转换，就不知道头到底占几个字，样本落点无法确定。

### 4.4 控制包与数据包：同一套头部的两种语义

#### 4.4.1 概念说明

本讲的学习目标之一是「认识控制包与数据包的区别」。现在可以收口了：在 VRT/CHDR 里，**数据包和控制包共用同一套头部格式**，区别仅在于头部 3 位的 `packet_type` 字段（见 4.1 的位布局表 bits 31–29）。

- **数据包**（`PACKET_TYPE_DATA = 0x0`）：负载是真实样本。会走 `recv`/`send` 的快路径，被拷贝转换后交给用户。
- **控制包 / context 包**（VRT 侧 `PACKET_TYPE_CONTEXT = 0x2`；CHDR 侧 `PACKET_TYPE_CMD/RESP/ERROR`）：负载是一个状态码，用来**异步**通告设备端事件——最常见的是「溢出（overrun）」「迟到命令」「broken chain」。它没有样本可拷，必须被拦截、转成 `rx_metadata_t::error_code`。

这种复用带来一个非常重要的工程结论：**设备的异常不是 C++ 异常，而是夹在数据流里的一个特殊包**。这就是 u2-l6 反复强调的「出错是常态而非异常，由 `error_code` 报告」的物理根源。

#### 4.4.2 核心流程

控制包如何变成 `error_code`（接收侧）：

```text
get_and_process_single_packet()
  └─ _vrt_unpacker(...) → 得到 ifpi.packet_type
  └─ ifpi.packet_type != DATA  →  返回 PACKET_INLINE_MESSAGE
get_aligned_buffs() 收到 PACKET_INLINE_MESSAGE:
  └─ get_context_code(vrt_hdr, ifpi)   # 取控制包负载首字的低 8 位
  └─ error_code = (rx_metadata_t::error_code_t)那个字节
        0x0 NONE | 0x1 TIMEOUT | 0x2 LATE_COMMAND
        0x4 BROKEN_CHAIN | 0x8 OVERFLOW | 0xc ALIGNMENT | 0xf BAD_PACKET
  └─ 若是 OVERFLOW → handle_overflow() + 打 "O" 日志
  └─ 把 metadata 交给用户（本次 recv 返回 0 个样本）
```

发送侧的「控制」语义则体现在 `tx_metadata_t` 上而非单独的控制包：用户用 `start_of_burst`/`end_of_burst`/`has_time_spec` 三个标志把「burst 边界与定时」编码进**数据包**的头部（bits 25/24/21-20），而不是另发一个控制包。欠流（underflow）这类发送侧异常则走另一条异步通道（`recv_async_msg`），不在本讲两个 handler 里。

#### 4.4.3 源码精读

- [host/include/uhd/transport/vrt_if_packet.hpp:39-53](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/vrt_if_packet.hpp#L39-L53)：`packet_type` 枚举——同一字段下并排列出「VRT 语言」与「CHDR 语言」两套取值，印证数据/控制复用同一 3 位。
- [host/lib/transport/super_recv_packet_handler.hpp:30-37](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L30-L37)：`get_context_code`——它对首字做 `word0 | byteswap(word0)` 再取低 8 位，是因为解析时尚不知字节序，用「或」把两种序都覆盖再掩码，鲁棒地取出 context 码。
- [host/lib/transport/super_recv_packet_handler.hpp:629-656](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L629-L656)：`PACKET_INLINE_MESSAGE` 分支——把 context 码翻译成 `error_code` 并处理 OVERFLOW。
- [host/include/uhd/types/metadata.hpp:113-136](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/metadata.hpp#L113-L136)：`error_code_t` 枚举取值——`NONE=0x0, TIMEOUT=0x1, LATE_COMMAND=0x2, BROKEN_CHAIN=0x4, OVERFLOW=0x8, ALIGNMENT=0xc, BAD_PACKET=0xf`。注意这些值与上面 `get_context_code` 取出的字节同构，故可直接强转。
- [host/lib/usrp/b200/b200_io_impl.cpp:286-298](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/b200/b200_io_impl.cpp#L286-L298)：真实接线示例——B200 在包一层 `b200_if_hdr_unpack_le`/`_pack_le`，先把 `link_type` 钉死为 `LINK_TYPE_CHDR` 再调通用 `vrt::if_hdr_unpack_le`。这是「设备决定链路层与字节序、handler 只管收发」分工的活样本。

#### 4.4.4 代码实践

**实践目标**：在源码层面把「设备端事件 → 控制包 → error_code」这条链走通。

**操作步骤**：

1. 在 `host/lib/transport/super_recv_packet_handler.hpp` 里找到 `get_context_code`（[L30-37](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L30-L37)）与它在 `PACKET_INLINE_MESSAGE` 分支的调用点（[L634-636](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L634-L636)）。
2. 打开 `host/include/uhd/types/metadata.hpp` 的 `error_code_t`（[L113-136](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/metadata.hpp#L113-L136)）。
3. 列一张映射表：设备若想通告「溢出」，它的控制包负载首字节的低 8 位应填什么值？这个值如何变成用户看到的 `metadata.error_code`？

**需要观察的现象**：context 码 `0x8` 经 `get_context_code` 取出后强转为 `ERROR_CODE_OVERFLOW`，于是用户在 `recv` 返回 0 样本时看到 `error_code == ERROR_CODE_OVERFLOW`，与 u2-l6 的接收循环逻辑闭环。

**预期结果**：能画出「设备 FPGA 检测到 overrun → 发一个 packet_type=CONTEXT、负载=0x8 的包 → handler 取低 8 位 → error_code=OVERFLOW → 用户 recv 收到 0 样本 + 该 error_code」的完整因果链。

#### 4.4.5 小练习与答案

**练习 1**：为什么设备的溢出/迟到命令不走 C++ 异常，而要发明一个「控制包」？

**参考答案**：因为这些事件发生在数据快路径上，且与具体某个样本的时间点强相关（「在 tsf=X 时发生了溢出」）。用夹在数据流里的控制包，能把事件**与它发生的时间戳一起**异步送达，且不阻塞快路径、不需要异常展开栈的开销。

**练习 2**：`get_context_code` 为什么要 `word0 | byteswap(word0)` 再取低 8 位？

**参考答案**：解析控制包那一刻还不知道线缆字节序。把字节序正常与字节序互换的两个值「或」起来，再取低 8 位，无论设备发的是大端还是小端，那个 context 码都会落到结果的低 8 位里，从而免去一次字节序判定。

## 5. 综合实践

把本讲四个模块串起来，做一次「手解一个 VRT/CHDR 包」的纸面演练。

**任务**：假设你在 B200（CHDR 链路层、小端）上接收，`tick_rate = 100e6`，某次 `recv()` 从传输层拿到一帧，其 `packet_buff[0]`（CHDR 头）解析后对应 VRT 头为：`packet_type=DATA`、`has_sid=true`、`has_tsf=true`、`tsf=123456789`、`packet_count=42`、`num_payload_words32=200`、`sob=true`、`eob=false`。

1. **画布局**：参照 4.1.2 的位布局表，画出该包的内存结构（header word + SID + TSF + payload），标注每个字段占几个字、`num_header_words32` 与 `num_packet_words32` 各是多少。
2. **走 handler**：这个包进入 `get_and_process_single_packet` 后会返回哪种 `packet_type`？为什么不会是 `INLINE_MESSAGE` 或 `SEQUENCE_ERROR`（假设 `_props[index].packet_count` 期望值恰好是 42）？
3. **算时间戳**：用户最终在 `rx_metadata_t` 里看到的 `time_spec` 是多少秒？（提示：`time_spec_t::from_ticks(123456789, 100e6)`。）
4. **对照源码验证**：用本讲给的永久链接，逐条核对你的推断——`copy_buff` 指向哪里（[L428-429](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L428-L429)）？时间戳在哪里转成 `time_spec`（[L699-701](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/super_recv_packet_handler.hpp#L699-L701)）？

**参考答案要点**：

1. 结构为 header(1) + SID(1) + TSF(2) + payload(200)，故 `num_header_words32=4`、`num_packet_words32=4+200=204`（CHDR 无 trailer）。
2. `packet_type=DATA` 所以不是 inline；期望序号 42 == 实际 42 所以不是 sequence error；返回 `PACKET_IF_DATA`，随后 `alignment_check` 把它当作（单通道情形下的）对齐锚点。
3. \( 123456789 / 100\times10^{6} \approx 1.23457 \) 秒。
4. `copy_buff = vrt_hdr + num_header_words32`（指向 payload 起点）；时间戳在 `get_aligned_buffs` 末尾用 `time_spec_t::from_ticks(curr_info[0].time, _tick_rate)` 转换。

若你有真实硬件并已构建 host，可进一步：用 `rx_samples_to_file`（u1-l6）加 `--stats` 之类的溢出统计，故意压低主机处理速度触发溢出，观察 stderr 上是否出现本讲讲的 `"O"` 快速日志字符——那是 `PACKET_INLINE_MESSAGE → OVERFLOW` 分支在真实流量里的足迹。**该硬件实验待本地验证。**

## 6. 本讲小结

- `vrt_if_packet` 是「线缆字节 ↔ C++ 结构」的翻译层：`if_packet_info_t` 是核心数据结构，`if_hdr_pack/unpack_{be,le}` 是四个对称的 API，靠 `link_type`（NONE/CHDR/VRLP）切换链路层、靠 `be/le` 切换字节序。
- 头部第一个字编码了 `packet_type`（bits 31–29）、各可选字段存在位（bits 20–28）、包序号（bits 19–16）与整包长度（bits 15–0）；生成器 `gen_vrt_if_packet.py` 把 7 个存在位的 \(2^7=128\) 种组合在编译期展开成跳转表，造就快路径。
- `super_recv_packet_handler`（`sph`）代表一组通道，在 `recv()` 里批量收包、解头、按时间戳做多通道对齐、把流控 ACK 包静默消费，并把夹在流里的控制包就地转成 `rx_metadata_t::error_code`。
- `super_send_packet_handler` 是镜像：把 `tx_metadata_t` 翻译成 VRT 头、超过单包上限时分片并按采样率累加时间戳、先 pack 头再 convert 样本最后 commit 触发发包，并用 metadata 缓存处理「SOB + 0 样本」。
- 控制包与数据包共用同一套头部，区别仅在 3 位 `packet_type`；设备的溢出/迟到等异常是夹在数据流里的控制包，经 `get_context_code` 取低 8 位强转成 `error_code`——这就是「出错是常态而非 C++ 异常」的物理根源。

## 7. 下一步学习建议

- **向下到设备实现**：本讲的 handler 是「设备无关」的。下一步读 `host/lib/usrp/b200/b200_io_impl.cpp` 与 `host/lib/usrp/usrp2/io_impl.cpp`，看具体设备如何接线 `set_vrt_unpacker/set_vrt_packer`、如何选定 `link_type` 与 `header_offset_words32`，把本讲的抽象落回真实硬件。
- **横向到 RFNoC 通路**：RFNoC 设备的数据通路不再走 `recv_packet_handler`，而是 CHDR 流图与 `link_if`。学完 u3 单元后回头对比，会看清「老 VRT 快路径」与「RFNoC 流图路径」的分野。
- **向上到收发示例**：回到 u2-l6/u2-l7，用本讲对 `error_code`、`sob/eob`、分片的理解重新审视 `rx_samples_to_file` 与 `tx_waveforms`，你会看到那些 `metadata` 字段是如何从这层一个个 VRT 包里浮现出来的。
- **验证型阅读**：通读 `host/tests/vrt_test.cpp` 与 `host/tests/sph_recv_test.cpp` 全部用例，它们是本讲所有结论的可执行规格说明。
