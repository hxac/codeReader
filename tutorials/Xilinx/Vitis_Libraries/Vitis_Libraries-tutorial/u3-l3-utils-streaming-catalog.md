# utils 流式原语目录

## 1. 本讲目标

前两讲（[u3-l1](u3-l1-hls-stream-and-dut.md)、[u3-l2](u3-l2-hls-pragmas.md)）我们掌握了三件事：`hls::stream` 与 end-flag 的配对约定、`ap_int` 宽位类型与 `.range()` 切片、以及 `pipeline/unroll/dataflow` 如何把 C++ 映射成硬件。这些都是「内功」。本讲是「兵器谱」——系统盘点 `utils` 库到底提供了哪些现成的流式原语，让你在搭数据通路时知道「有什么现成轮子可用」。

学完本讲你应当能够：

- 说出 `utils` 各流式原语的名字、职责与典型使用场景；
- 解释 AXI 主端口（宽）与 `hls::stream`（窄）之间为什么需要专门的转换原语，以及转换时发生的「位宽切分 / 拼接」；
- 区分三类原语——存储边界转换（AXI↔stream）、流形重塑（dup/combine/split/reorder/sync/discard）、片上存储（UramArray/cache）；
- 学会用这些原语组合出一个「DDR 读入 → 分发到多路内核 → 收集 → 写回 DDR」的数据分发/收集结构。

## 2. 前置知识

本讲默认你已经读过 [u3-l1](u3-l1-hls-stream-and-dut.md) 与 [u3-l2](u3-l2-hls-pragmas.md)。这里快速回顾三个会反复用到的概念：

- **`hls::stream<T>`**：单向 FIFO，元素只能顺序、单次读写，天然映射 II=1 的硬件流水线。流本身不携带长度信息，所以几乎每个数据流都要配一条 `hls::stream<bool>` 的 **end-flag 流**（`false` 表示还有数据，`true` 表示结束）。
- **`ap_int<N>` 与 `.range(hi, lo)`**：任意位宽整数。`.range()` 是把一个宽 beat 切成多路窄通道、或把多路窄通道拼成宽 beat 的「切片刀」。位宽直接放大带宽。
- **`#pragma HLS`**：`DATAFLOW` 把多个子函数用 stream 串成任务级流水；`pipeline II=1` 决定吞吐；`unroll` 换空间并行；`array_partition complete` 把 RAM 拆开以匹配 unroll 的端口需求。

另外补充两个本讲会用到的工程常识：

- **AXI 主端口（AXI master）**：内核访问片外 DDR/HBM 的宽总线接口，位宽通常是 2 的幂（8/16/32/64/128/256/512/1024-bit）。一次突发传输（burst）能搬一大批数据，是「宽而批量」的。
- **`hls::stream` 接口**：内核与内核之间的窄 FIFO，常常只有 8/16/32-bit，是「窄而逐拍」的。

宽 AXI 与窄 stream 之间的「位宽不匹配」就是 AXI↔stream 原语要解决的核心矛盾。

## 3. 本讲源码地图

本讲涉及的头文件全部位于 `utils/L1/include/xf_utils_hw/` 下。下表按「职能分类」列出，方便查阅：

| 分类 | 头文件 | 提供的原语 | 一句话职责 |
|---|---|---|---|
| 存储边界 | `axi_to_stream.hpp` | `axiToStream` / `axiToCharStream` | 从 AXI 主端口（DDR）读数据，切成窄元素喂给 stream |
| 存储边界 | `stream_to_axi.hpp` | `streamToAxi` | 把窄 stream 拼成宽 AXI beat，突发写回 DDR |
| 流形重塑 | `stream_dup.hpp` | `streamDup` | 一路输入复制成多路相同输出（u3-l1 已详解） |
| 流形重塑 | `stream_combine.hpp` | `streamCombine` | 多路窄 stream 拼成一路宽 stream |
| 流形重塑 | `stream_split.hpp` | `streamSplit` | 一路宽 stream 切成多路窄 stream |
| 流形重塑 | `stream_reorder.hpp` | `streamReorder` | 在固定窗口内重排元素顺序（如 RGB→BGR） |
| 流形重塑 | `stream_sync.hpp` | `streamSync` | 多路 stream 锁步对齐，合并 end 标志 |
| 流形重塑 | `stream_discard.hpp` | `streamDiscard` | 把不再需要的数据流「排空」丢弃 |
| 跨类型/跨流 | `multiplexer.hpp` | `Multiplexer` / `makeMux` | 用一条物理 FIFO 串行收发不同类型的数据 |
| 片上存储 | `uram_array.hpp` | `UramArray` | 带前递缓存的 URAM 数组，解决迭代间 RAW 依赖 |
| 片上存储 | `cache.hpp` | `cache::readOnly` | 只读 DDR/HBM 的片上 URAM 缓存，降低随机访问的访存 |
| 辅助 | `enums.hpp` | `LSBSideT` / `MSBSideT` 等标签类 | 用参数类型（而非值）来选择算法重载 |

此外，`utils/L1/tests/` 下每个原语都有同名或近名的用例目录（如 `stream_combine/combine_lsb`、`stream_split/split_lsb`、`axi_to_stream`、`stream_to_axi`、`multiplexer`、`uram_array`），它们是本讲代码实践的落脚点。

## 4. 核心概念与源码讲解

本讲把全部原语归为四个最小模块依次讲解。

### 4.1 AXI↔stream 转换原语：`axiToStream` 与 `streamToAxi`

#### 4.1.1 概念说明

内核计算用的是窄 stream，但数据真正的家在片外 DDR/HBM，只能通过宽 AXI 主端口访问。于是任何上板系统在「入口」和「出口」都需要两个搬运器：

- **入口（读）**：`axiToStream`——从 AXI 主端口把一段缓冲读出来，切成一个个窄元素，喂给下游 stream，并自动生成 end-flag。
- **出口（写）**：`streamToAxi`——把上游窄 stream 的元素拼回宽 AXI beat，按突发写回 DDR。

两者共同的核心难题是 **位宽适配**：AXI 宽度 `_WAxi` 与 stream 元素宽度通常不等。设

\[
\text{scal\_vec} = \frac{\_WAxi}{8 \times \text{sizeof}(T)}
\]

即「一个 AXI beat 能装下几个 stream 元素」。读时要把一个 beat **拆**成 `scal_vec` 个元素；写时要把 `scal_vec` 个元素 **拼**成一个 beat。`axiToCharStream` 则更放松——它按 8-bit char 对齐，支持任意字节偏移 `offset`，适合处理不对齐的字节流。

#### 4.1.2 核心流程

`axiToStream` 内部用 `DATAFLOW` 把工作分成两个并发阶段：

```
   ap_uint<_WAxi>* rbuf  ──►  read_to_vec  ──►  vec_strm(FIFO)  ──►  split_vec  ──►  ostrm + e_ostrm
   (DDR 缓冲, 宽)            (整 beat 突发读)     (宽 beat 流)        (切窄 + 加 end)
```

1. `read_to_vec`：按 burst 把宽 beat 逐个读进内部 FIFO，循环体 **不带判断** 地 `pipeline II=1`，这样工具才能推导出正确的 burst 长度。
2. `split_vec`：把每个宽 beat 用 `.range()` 切成 `scal_vec` 个窄元素写入输出流，并在末尾写一个 `true` 的 end 标志。

`streamToAxi` 则反向，分两阶段：

```
   istrm + e_istrm  ──►  countForBurst  ──►  axi_strm + nb_strm  ──►  burstWrite  ──►  wbuf (DDR)
   (窄 stream)          (拼宽 + 数 burst)     (宽 beat + burst 计数)    (按 burst 写 AXI)
```

1. `countForBurst`：把 `_WAxi/_WStrm` 个窄元素拼成一个宽 beat，同时累计每段 burst 的长度。
2. `burstWrite`：按 burst 计数把宽 beat 写回 AXI 缓冲。

#### 4.1.3 源码精读

**对齐版 `axiToStream` 的接口**（要求 AXI 宽度是元素宽的整数倍）：

[utils/L1/include/xf_utils_hw/axi_to_stream.hpp:L75-L76](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/axi_to_stream.hpp#L75-L76) —— 接收 AXI 缓冲指针 `rbuf`、要读的元素个数 `num`，输出数据流 `ostrm` 与 end 流 `e_ostrm`。

**实现体：静态断言 + DATAFLOW 双阶段**：

[utils/L1/include/xf_utils_hw/axi_to_stream.hpp:L410-L430](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/axi_to_stream.hpp#L410-L430) —— 先用 `XF_UTILS_HW_STATIC_ASSERT` 在编译期卡住两个约束：`_WAxi % sizeof(_TStrm)==0` 且 `_WAxi` 必须是 8 到 1024 的 2 的幂；然后用 `DATAFLOW` 把 `read_to_vec` 与 `split_vec` 并起来，中间用深度为 `_BurstLen*2` 的 LUTRAM FIFO 缓冲。`scal_vec = _WAxi/(8*sizeof(_TStrm))` 就是「每个 beat 含几个元素」。

**`split_vec` 切片逻辑**：

[utils/L1/include/xf_utils_hw/axi_to_stream.hpp:L175-L213](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/axi_to_stream.hpp#L175-L213) —— 注意它对 **第一个** beat 单独处理（因为可能有起始偏移 `offset_AL`），其余 beat 走主循环 `SPLIT_VEC`，每拍用 `vec.range(WStrm*(j+1)-1, WStrm*j)` 切出第 `j` 个元素；循环结束后写 `e_strm.write(true)` 注入 end 标志。

**`streamToAxi` 的拼接 + 突发写**：

[utils/L1/include/xf_utils_hw/stream_to_axi.hpp:L70-L116](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_to_axi.hpp#L70-L116) —— `countForBurst` 每读够 `N=_WAxi/_WStrm` 个窄元素就拼出一个宽 beat 写入 `axi_strm`，并按 `_BurstLen` 计数；不足一个 beat 的尾部补零。

[utils/L1/include/xf_utils_hw/stream_to_axi.hpp:L149-L164](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_to_axi.hpp#L149-L164) —— 顶层同样先做 `_WAxi % _WStrm == 0` 的静态断言，再用 `DATAFLOW` 串联 `countForBurst` 与 `burstWrite`。

#### 4.1.4 代码实践（源码阅读型 + 可选运行）

1. **目标**：理解 `scal_vec` 如何随位宽变化，看清「宽 beat 切窄元素」的字节布局。
2. **步骤**：
   - 打开 `utils/L1/include/xf_utils_hw/axi_to_stream.hpp`，在 `axiToStream` 实现体（L410-L430）里找到 `scal_vec` 与 `scal_char` 两个常量，手算：当 `_WAxi=512`、`_TStrm=ap_uint<32>` 时，`scal_vec` 是多少？每个 beat 切出几个 32-bit 元素？
   - （可选运行）进入 `utils/L1/tests/axi_to_stream`，执行 `make run TARGET=csim`。该用例的 testbench 是 `axi_to_stream_tb.cpp`，csim 是最轻量、不综合的纯软件仿真。
3. **观察现象**：csim 会打印测试数据并给出 `PASS`/`FAIL`。
4. **预期结果**：`scal_vec = 512/32 = 16`，即一个 512-bit beat 切出 16 个 32-bit 元素；csim 应输出 `PASS`。**待本地验证** csim 的实际终端输出。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `read_to_vec` 的循环里强调「不能带判断」？  
  **答**：带分支会让 HLS 工具无法确定每次访问的连续性，从而推导不出正确的 burst 长度，导致 AXI 读退化成单拍传输，带宽骤降。

- **练习 2**：`axiToStream` 与 `axiToCharStream` 的关键区别是什么？  
  **答**：前者要求数据元素按自身大小对齐，按元素个数 `num` 读；后者放宽到 8-bit char 对齐，按字节数 `len` 读并支持字节 `offset`，适合不对齐的字节流，最后一个 beat 的高位可能填无效数据。

---

### 4.2 流形重塑（一）：`streamSplit` 与 `streamCombine`

#### 4.2.1 概念说明

`stream_dup`（[u3-l1](u3-l1-hls-stream-and-dut.md) 详解过）解决「**一路变多路相同**」。而 `split` 与 `combine` 解决的是「**宽窄互转**」：

- **`streamSplit`**：把 **一路宽** stream 拆成 **多路窄** stream——典型场景是 AXI 读进来一个 512-bit beat，要分发给 4 个各吃 128-bit 的并行内核。
- **`streamCombine`**：把 **多路窄** stream 拼成 **一路宽** stream——典型场景是 4 个内核各吐 32-bit，要拼成 128-bit 写回 AXI。

两者都支持 `LSBSideT`（从最低位 LSB 起）和 `MSBSideT`（从最高位 MSB 起）两种对齐方向，靠的是 `enums.hpp` 里的 **标签类**。标签类是一种常见 HLS 技巧：用一个空 struct 类型作为函数参数，让编译器据「类型」而非「值」来选择重载，避免模板推断歧义。

[utils/L1/include/xf_utils_hw/enums.hpp:L49-L55](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/enums.hpp#L49-L55) —— `LSBSideT` 与 `MSBSideT` 就是两个空 struct，纯粹用来选重载。

#### 4.2.2 核心流程

`streamSplit`（LSB 版）每拍做：

```
读 1 个 ap_uint<_WIn> 宽 beat
  for i in [0, _NStrm):            // unroll 全展开
      d = data.range((i+1)*_WOut-1, i*_WOut)
      ostrms[i].write(d)            // 第 i 路拿到第 i 段
  e_ostrm.write(false)              // 跟随输入 end
```

`streamCombine`（简单拓宽版，LSB）每拍反向：把 `_NStrm` 路各读一个 `_WIn` 元素，拼进 `cmb.range((i+1)*_WIn-1, i*_WIn)`，输出一个 `ap_uint<_WOut>`（`_WOut >= _WIn*_NStrm`，多余高位补零）。

注意位宽约束：`split` 要求 `_WIn >= _WOut*_NStrm`（多了丢弃），`combine` 要求 `_WOut >= _WIn*_NStrm`（多了补零）。

#### 4.2.3 源码精读

**`streamSplit` 的 LSB 实现**，含一个极佳的位宽示例：

[utils/L1/include/xf_utils_hw/stream_split.hpp:L90-L125](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_split.hpp#L90-L125) —— 注释里给了具体例子：`_WIn=20, _WOut=4, _NStrm=4`，输入 `0x82356`，LSB 版拆出 `ostrms[0..3] = 0x6,0x5,0x3,0x2`，最高位 `0x8` 因超出 `_WOut*_NStrm=16` 位被丢弃。循环 `pipeline II=1`，内层 `unroll` 全展开，一拍完成拆分。

**`streamCombine` 的简单拓宽实现（LSB）**：

[utils/L1/include/xf_utils_hw/stream_combine.hpp:L287-L318](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_combine.hpp#L287-L318) —— 注释画出布局：4 路 stream 纵向看是 `0,1,2,3 / 4,5,6,7 / 8,9,a,b`，拼出的宽 stream 是 `3210,7654,ba98`（第 0 路落在最低位段）。

**`streamCombine` 的 one-hot 选择版**（带 `select_cfg`）：

[utils/L1/include/xf_utils_hw/stream_combine.hpp:L156-L219](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_combine.hpp#L156-L219) —— 这个重载更复杂：`select_cfg` 是 one-hot 编码，LSB 对应 `istrms[0]`；被选中的路会被「压缩」到输出的一侧（LSB 或 MSB），未被选中的路不占用输出位。它用了一个 `_NStrm × _NStrm` 的展开数组 `tmp`/`b` 做「列压缩」——本质是把选中的列往一端靠拢。这种重载适合「N 路里动态挑 K 路拼接」的场景。

#### 4.2.4 代码实践（运行型）

1. **目标**：跑通一个 combine 用例，亲眼看「多路窄流拼成一路宽流」，并用一句话描述其数据流语义。
2. **操作步骤**：
   - 进入用例目录：`utils/L1/tests/stream_combine/combine_lsb`。
   - 该用例的 DUT 是 `test_core_comb_lsb`，调用 `streamCombine<16, 128, 4>(..., LSBSideT())`——4 路 16-bit 拼成 1 路 128-bit（多出的 64-bit 补零）。见 [utils/L1/tests/stream_combine/combine_lsb/test.cpp:L28-L34](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_combine/combine_lsb/test.cpp#L28-L34)。
   - 执行 `make run TARGET=csim`。
3. **观察现象**：csim 会打印 `WIN_STRM=16 / WOUT_STRM=128 / NSTRM=4 / NS=8192`，最后给出 `PASS: no error found.` 或 `FAIL`。
4. **预期结果**：`PASS`。一句话语义：「4 路 16-bit 输入流，按 `0,1,2,3` 顺序每拍拼进一个 128-bit 字的低 64 位，高 64 位补零，end 标志合并为一路。」**待本地验证** 终端是否输出 PASS。
5. **进阶**：把目录切到 `stream_combine/combine_msb`，对比 LSB 与 MSB 版输出布局的差异。

#### 4.2.5 小练习与答案

- **练习 1**：`streamSplit<_WIn=512, _WOut=128, _NStrm=4>` 会产生几路、每路多宽？  
  **答**：4 路，每路 128-bit，正好填满 512-bit 输入，无丢弃。

- **练习 2**：为什么 `streamCombine` 用 `LSBSideT`/`MSBSideT` 标签类而不是用一个 `bool` 参数？  
  **答**：用「类型」选重载可以避免模板参数推断歧义，且标签类是空 struct、零运行时开销；用 `bool` 则会和其它模板参数纠缠，编译器难以区分重载。

---

### 4.3 流形重塑（二）：`Multiplexer`、`streamReorder`、`streamSync`、`streamDiscard`

#### 4.3.1 概念说明

这一组原语解决更「杂」的需求：

- **`Multiplexer`**：一条物理 FIFO 上 **串行收发不同类型** 的数据。例如控制流里既要传配置参数（int）又要传数据（ap_uint），可以共用一条 `ap_uint<W>` 总线，发送端 `put<int>(x)` 再 `put<ap_uint<32>>(y)`，接收端按相同顺序 `get`。它内部用 union 把任意类型 `T` reinterpret 成 `ap_uint<sizeof(T)*8>`，再按总线宽度 `W` 分拍传输。
- **`streamReorder`**：在固定大小 `_WindowSize` 的窗口内 **重排元素顺序**。经典例子：RGB 按序复用在一条流上（R-G-B-R-G-B…），下游却要 B-G-R 顺序，于是用窗口大小 3、配置 `2,1,0` 做窗口内倒序。
- **`streamSync`**：**锁步对齐** 多路 stream——确保 N 路的第 k 个元素同时到达下游，并把 N 条 end 流合并成一条。要求各路元素数相同，否则永不终止。
- **`streamDiscard`**：**排空丢弃** 不再需要的数据流（连同其 end 流），避免 FIFO 被无人读的数据塞满而反压死。

#### 4.3.2 核心流程

`Multiplexer` 是个类模板，由 `MuxSide`（`MUX_SENDER` 发送端 / `MUX_RECEIVER` 接收端）和总线宽度 `W` 参数化：

```
发送端:  makeMux<MUX_SENDER, W>(fifo)  ──►  mux.put<T>(val)   // T → ap_uint → 可能分多拍写 fifo
接收端:  makeMux<MUX_RECEIVER, W>(fifo) ──►  T v = mux.get<T>() // 可能多拍读 fifo → ap_uint → T
```

`put<T>`/`get<T>` 用 `(v.width-1)/W + 1` 算出「传一个 T 需要几拍总线」，并用 `static_assert` 保证发送端不能 `get`、接收端不能 `put`。

`streamSync` 每拍从 N 路各读一个元素、各读一个 end 位，用 `ap_uint<_NStrm>` 累积「是否全部结束」，全部结束时写一条合并的 `true`。

`streamReorder` 启动时一次性从 `order_cfg` 读入 `_WindowSize` 个目标下标，之后每读满一个窗口就按配置顺序输出。

#### 4.3.3 源码精读

**`Multiplexer` 类与 `MuxSide` 枚举**：

[utils/L1/include/xf_utils_hw/multiplexer.hpp:L84-L97](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/multiplexer.hpp#L84-L97) —— `enum MuxSide { MUX_SENDER, MUX_RECEIVER }`；`Multiplexer<S,W>` 持有一条 `hls::stream<ap_uint<W>>&` 引用，构造函数只允许从已有 FIFO 包装（默认构造被 `delete`）。

**`put`（发送端）**：

[utils/L1/include/xf_utils_hw/multiplexer.hpp:L141-L153](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/multiplexer.hpp#L141-L153) —— `static_assert(S==MUX_SENDER, ...)` 保证方向正确；用 `details::as_ap_uint<T>::cast(d)` 把 `T` 经 union reinterpret 成 `ap_uint`，再按 `W` 位一段 `unroll` 写入 FIFO。`get`（接收端）逻辑对称，见同文件 L114-L127。

**`makeMux` 工厂**：

[utils/L1/include/xf_utils_hw/multiplexer.hpp:L166-L170](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/multiplexer.hpp#L166-L170) —— 模板参数 `S` 必须显式给，`W` 可由传入的 FIFO 推断。

**`streamReorder` 的语义文档**（RGB→BGR 例子）：

[utils/L1/include/xf_utils_hw/stream_reorder.hpp:L34-L62](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_reorder.hpp#L34-L62) —— 注意它假设「流过的元素总数是窗口大小的整数倍」，否则会卡死。

**`streamSync` 的屏障语义**：

[utils/L1/include/xf_utils_hw/stream_sync.hpp:L34-L54](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_sync.hpp#L34-L54) —— 文件头标注 `Barrier-like logic`，要求各输入流元素数相同。

**`streamDiscard` 的两种重载**：

[utils/L1/include/xf_utils_hw/stream_discard.hpp:L34-L56](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_discard.hpp#L34-L56) —— 一种每路各自带 end 流，另一种 N 路共享一条 end 流。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理解 `Multiplexer` 如何用 union 在一条 FIFO 上串行传不同类型。
2. **步骤**：打开 `utils/L1/include/xf_utils_hw/multiplexer.hpp`，阅读 `details::as_ap_uint<T>::cast`（L44-L62）与 `put`/`get`。回答：当总线宽度 `W=16`、要传一个 `T=uint32_t`（32-bit）时，`put` 会在 FIFO 上写几拍？
3. **观察现象**：用公式 `(v.width-1)/W + 1 = (32-1)/16 + 1 = 2` 拍。
4. **预期结果**：2 拍，每拍 16-bit，接收端 `get<uint32_t>` 同样读 2 拍再拼回。
5. （可选）`utils/L1/tests/multiplexer` 提供了完整 testbench，可执行 `make run TARGET=csim` 验证行为。**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：`streamReorder` 在什么前提下会卡死？  
  **答**：当流过的元素总数不是窗口大小 `_WindowSize` 的整数倍时，最后一个不完整窗口无法填满，模块会一直等待而挂起。

- **练习 2**：`streamSync` 为什么要求各输入流元素数相同？  
  **答**：它是锁步屏障——每拍必须同时从 N 路各取一个元素；若某路先结束，其它路仍会试图读它，造成死锁或无法终止。

---

### 4.4 片上存储：`UramArray` 与只读 `cache`

#### 4.4.1 概念说明

当内核需要一张查找表或一个工作数组时，可以把它放在片上存储。`utils` 提供两个面向 URAM（UltraRAM，FPGA 上密度最高的片上 RAM）的助手：

- **`UramArray`**：一个带 **前递缓存（forwarding regs）** 的 URAM 数组类，提供 `memSet/write/read` 接口。它解决一个经典 HLS 痛点：URAM 的读延迟 > 1 拍，若同一地址「先写后读」（RAW，Read-After-Write），跨迭代依赖会把 II 从 1 拉高。前递缓存保留最近 `_NCache` 次写入的地址与值，命中时直接返回、跳过 URAM 读取，从而保住 II=1。
- **`cache`**：一个 **只读 DDR/HBM 的片上 URAM 缓存**。当下游对片外内存做 **随机访问** 时，同一 cache 行可能被反复读取；`cache` 把最近从 DDR 载入的行存在 URAM 里，命中即免一次 DDR 往返，显著降低随机访问的访存带宽。它有单缓冲（`ddrMem`）与双缓冲（`ddrMem0/ddrMem1`，对应双 DDR bank）两种 `readOnly` 重载。

两者的关键相似处：都显式处理 **迭代间依赖** 以维持 `pipeline II=1`——`UramArray` 靠前递寄存器，`cache` 靠 `#pragma HLS DEPENDENCE ... false` 告诉工具「这些数组在 distance=1 上没有真依赖」，并自己用小队列维护一致性。

#### 4.4.2 核心流程

`UramArray` 的 `read(index)` 流程：

```
1. 查前递缓存 _state[0.._NCache-1]（保存最近写入的 (index, value)）
   if index == _index[i]:  return _state[i]      // 命中，跳过 URAM 读
2. 未命中：按 _WData<=72 或 >72 两种布局，从 blocks[...] 读出并返回
```

`write(index, d)` 流程：写 URAM 后，把 `(index, d)` 推入前递缓存（整体下移一格，`_state[0]` 放最新值）。

`cache::readOnly`（单缓冲，带 end）每拍：

```
读 addrStrm 得 index
  → 把 index 拆成 (k00,k01,k10,k20) 定位到片上行
  → 查 valid 标志 + 地址标签：命中则从 onChipRam0 取；未命中则从 ddrMem 取并回填
  → 把对应元素切片写入 dataStrm
```

#### 4.4.3 源码精读

**`UramArray` 类声明与构造（含 URAM 绑定）**：

[utils/L1/include/xf_utils_hw/uram_array.hpp:L86-L97](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/uram_array.hpp#L86-L97) —— 构造函数里 `#pragma HLS bind_storage variable=blocks type=RAM_2P impl=URAM` 把数组绑定到 UltraRAM，并对 `blocks` 的两个维度做 `complete` 全分区以匹配并行访问。文件头注释（L57-L85）解释了前递缓存机制与「让 HLS 忽略 blocks 迭代间依赖」的必要性。

**`write`：写 URAM + 维护前递缓存**：

[utils/L1/include/xf_utils_hw/uram_array.hpp:L235-L270](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/uram_array.hpp#L235-L270) —— 先按位宽布局写 `blocks`，再在 `Write_Cache` 循环里把 `_state`/`_index` 整体下移，把最新写入放到 `_state[0]`。

**`read`：先查缓存、未命中再读 URAM**：

[utils/L1/include/xf_utils_hw/uram_array.hpp:L272-L310](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/uram_array.hpp#L272-L310) —— `Read_Cache` 循环遍历 `_NCache` 个槽，命中即提前 return，从而打断跨迭代 RAW 长依赖链。

**`cache` 类与模板参数**：

[utils/L1/include/xf_utils_hw/cache.hpp:L27-L51](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp#L27-L51) —— 注释明确「cache is a URAM design for caching Read-only DDR/HBM」，模板参数有 8 个：数据类型 `T`、每片 RAM 行数 `ramRow`、片上 RAM 分组数 `groupRamPart`、每个 512 含几个数据 `dataOneLine`、地址宽度 `addrWidth`，以及 valid/addr/data 三套数组各自的 RAM 类型（0=LUTRAM/1=BRAM/2=URAM）。注意 `T` 不能是 float/double。

**`readOnly`（单缓冲，带 end）的主循环**：

[utils/L1/include/xf_utils_hw/cache.hpp:L153-L188](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp#L153-L188) —— 接口接收 512-bit DDR 指针、地址流、输出数据流；主循环 `pipeline II=1`，并用三句 `#pragma HLS DEPENDENCE variable=... type=inter direction=RAW distance=1 true` 显式声明依赖，让工具敢于做 II=1 调度。为维持正确性，实现里维护了 `addrQue/pingQue/validQue/...` 等 depth=4 的小队列做一致性前递（这与 `UramArray` 的前递缓存思想一致）。

**双 DDR 的 `readOnly` 重载**：

[utils/L1/include/xf_utils_hw/cache.hpp:L413-L418](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/cache.hpp#L413-L418) —— 同时索引两片 off-chip buffer（`ddrMem0/ddrMem1`），输出两路数据流，对应 `cache_ro_2DDR_with_e` 用例（见 [u12-l2](u12-2-resource-memory-banking.md) 对双 DDR 带宽的讨论）。

#### 4.4.4 代码实践（源码阅读型 + 可选运行）

1. **目标**：看清 `UramArray` 如何用前递缓存把 RAW 依赖从 URAM 读路径上「短路」掉。
2. **步骤**：
   - 打开 `utils/L1/include/xf_utils_hw/uram_array.hpp`，对比 `write`（L235-L270）与 `read`（L272-L310）。
   - 找出：`_NCache` 这个模板参数控制什么？为什么文档说它「应在初次综合后再定」？
   - （可选运行）`utils/L1/tests/uram_array` 是完整用例（含 `dut.cpp/dut.hpp`），执行 `make run TARGET=csim` 验证功能，执行 `make run TARGET=csynth` 在综合报告里观察是否达成 II=1。
3. **观察现象**：csim 应 PASS；csynth 报告里 `read`/`write` 所在循环的 II 应为 1。
4. **预期结果**：`_NCache` = 前递缓存深度，等于 URAM 实际写延迟拍数；设小了会因缓存未命中走回 URAM 长路径而抬升 II，设大了浪费寄存器，故需综合后按实际延迟微调。**待本地验证** csynth 的 II 数值。

#### 4.4.5 小练习与答案

- **练习 1**：`UramArray` 的前递缓存和 CPU 的 cache 有何本质不同？  
  **答**：它不是为「命中率」设计的容量缓存，而是为打断「写后读」RAW 依赖链、保住 II=1 而设的固定深度前递寄存器；深度由 URAM 写延迟决定，与访问局部性无关。

- **练习 2**：`cache` 类为什么用 `#pragma HLS DEPENDENCE ... distance=1 true`？  
  **答**：`readOnly` 每拍可能写 `valid/onChipRam0/onChipAddr`（回填），又每拍读它们（查命中），工具会保守认为存在 distance=1 的 RAW 依赖而把 II 抬高；该 pragma 显式声明依赖，配合实现里的小前递队列保证正确性，从而维持 II=1。

---

## 5. 综合实践

把本讲四类原语串起来，画一个 **典型的数据分发/收集系统框图**（纯纸笔设计，不需运行）：

> 一批数据在 DDR，需要喂给 4 个并行的计算内核，算完再写回 DDR。

请按下列要求完成设计并在图上标注使用的原语名：

1. **入口搬运**：用一个原语把 DDR 的宽 AXI 缓冲读成 stream。（应选 `axiToStream`）
2. **分发**：把一路宽 stream 拆成 4 路窄 stream 喂给 4 个内核。（应选 `streamSplit`，注意 `_WIn/_WOut/_NStrm` 的位宽关系）
3. **同步**：若 4 个内核要求输入锁步对齐，在分发与内核之间插入一个对齐原语。（应选 `streamSync`）
4. **收集**：4 路输出拼回一路宽 stream。（应选 `streamCombine`）
5. **出口搬运**：写回 DDR。（应选 `streamToAxi`）

完成后再回答两个进阶问题：

- 如果某一路的输出下游暂时不消费，为避免反压死整条流水，应在何处放一个 `streamDiscard`？
- 如果内核对 DDR 的访问是 **随机地址** 而非顺序，应该用 `cache` 还是 `UramArray`？为什么？

参考答案：`streamDiscard` 应放在「不消费的输出路」上把它排空；随机访问应选 `cache`，因为 `cache` 专门缓存只读 DDR/HBM 的行以降低随机访存，而 `UramArray` 是内核内部的私有工作数组、不直接面向 DDR。

## 6. 本讲小结

- `utils` 的流式原语可分三类：**存储边界转换**（`axiToStream`/`streamToAxi`）、**流形重塑**（`dup`/`split`/`combine`/`reorder`/`sync`/`discard`）、**片上存储**（`UramArray`/`cache`）。
- AXI↔stream 原语的核心是 **位宽适配**：`scal_vec = _WAxi / WStrm` 个元素拼/拆一个 beat，靠 `DATAFLOW` 把「突发读/写」与「切分/拼接」两阶段并起来。
- `split`/`combine` 用 `LSBSideT`/`MSBSideT` **标签类** 选对齐方向，零开销；`combine` 还有 one-hot 动态选择重载。
- `Multiplexer` 用 union + 类型分拍，在一条 FIFO 上串行收发不同类型数据；`reorder`/`sync`/`discard` 分别处理窗口重排、锁步对齐、排空丢弃。
- `UramArray` 与 `cache` 都靠 **前递/小队列** 显式打断迭代间 RAW 依赖以维持 II=1，前者是内核私有 URAM 数组，后者是只读 DDR/HBM 的 URAM 缓存。
- 所有原语都在 `utils/L1/tests/` 下有同名用例，`make run TARGET=csim` 是验证它们行为的最轻量入口。

## 7. 下一步学习建议

- **横向应用**：本讲只看了 `utils` 的原语；接下来可以去 [u5-l2](u5-2-data-movers.md) 看 `data_mover` 库如何把 `mm2s`/`s2mm` 与这些原语组合成更高层的 4D 搬运器，以及在 [u6-l3](u6-l3-vss-fft-ifft-example.md) 的端到端 AIE 示例里观察 `axiToStream`/`streamToAxi` 在真实系统中的位置。
- **纵向深挖**：若想深入 `UramArray`/`cache` 如何通过依赖分析与存储分块保住 II=1，建议学习 [u12-l1](u12-1-dataflow-ssr-ii.md)（dataflow/SSR/II 调优）与 [u12-l2](u12-2-resource-memory-banking.md)（URAM、HBM/DDR 分区与报告解读），并对照 `utils/L1/tests/cache_ro_1DDR_with_e` 与 `cache_ro_2DDR_with_e` 两个用例。
- **自己动手**：在学完 [u14-l2](u14-2-write-your-own-kernel.md) 后，可尝试用本讲的原语拼一个「DDR→split→两路内核→combine→DDR」的最小系统，跑通 csim 再看 csynth 报告。
