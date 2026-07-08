# eepnet 配置数组格式解析

## 1. 本讲目标

上一讲（u3-l2）我们看到 `eepBinCvt` 把 `*.pub.bin` 拆成裸机「三件套」：`eepnet.h`、`eepnet.mem`、`eepinput.mem`。其中 `eepnet.h` 里那个看似杂乱的 `eepnet_config[]` 字节数组，其实是整个 TPU 网络的「元数据说明书」——它告诉裸机程序：网络有几段内存、输入输出张量长什么样、预处理参数是多少、算法表放在哪里。

本讲学完后，你应该能够：

1. 说出 `eepnet_config[]` 数组各字段在内存中的排列顺序与含义。
2. 看懂 `EEPTPU_SA::eeptpu_init` 如何用「指针游走法」逐字段解析这个数组。
3. 区分 `bin_type`（enc=1/pub=2）等关键字段如何改变解析路径。
4. 从一段十六进制字节里手动解码出输入分辨率、通道数、mean/norm 等信息。
5. 理解「注释描述的格式」与「代码实际读取的格式」之间的细微差异，学会以代码为准。

## 2. 前置知识

- **裸机为什么需要这个数组**：Linux 路线有运行库 `libeeptpu_pub`，能在 `load_bin` 时动态解析 bin 内部的元数据；裸机没有这个运行时解析器，也没有标准文件系统，所以必须把解析工作前置到开发主机上，由 `eepBinCvt` 把元数据「翻译」成一个 C 数组 `eepnet_config[]`，编译期 `#include` 进 ELF。详见 u3-l2。
- **NCHW 与 shape**：深度学习张量常用 `(N, C, H, W)` 四元组描述，分别表示 batch、通道数、高、宽。本讲里 `shape[0..3]` 就对应这四维。
- **定点数与 exp**：TPU 内部用 16 位定点整数存储张量。要把定点值还原成浮点，需除以一个 2 的幂：

  \[
  x_{\text{float}} = \frac{x_{\text{int16}}}{2^{\text{exp}}}
  \]

  这个幂次 `exp` 也被记录在配置数组里。输入侧的 `exp` 表示输入数据被放大了多少倍存进硬件；输出侧的 `exp` 表示读出的定点结果要除以多少。
- **小端字节序**：本数组按 little-endian 存储整数。例如字节序列 `0x01,0x00,0x00,0x00` 表示 32 位整数 `1`。
- **两个魔法地址**：`EEPTPU_MEM_BASE_ADDR`（数据区基址 `0x31000000`）与 `EEPTPU_REG_BASE_ADDR`（寄存器基址 `0xA0000000`），由硬件设计定死，详见 u1-l3、u4-l2。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sdk/standalone/src/net_data/eepnet.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/net_data/eepnet.h) | `eepBinCvt` 自动生成的元数据数组 `eepnet_config[]`，本讲的主角。 |
| [sdk/standalone/src/eeptpu/eeptpu_sa.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp) | 内含 `eeptpu_init` 解析逻辑、顶部的格式注释，以及 `epmat2nmat` 等使用 shape/exp 的函数。 |
| [sdk/standalone/src/eeptpu/eeptpu_sa.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h) | 定义 `st_hwaddr_info` 结构与 `EEPTPU_SA` 类的成员（`addr_out`/`addr_in`/`mean`/`norm` 等）。 |
| [sdk/standalone/src/config.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h) | 定义 `EEPTPU_MEM_BASE_ADDR`/`EEPTPU_REG_BASE_ADDR`，覆盖数组里的地址字段。 |
| [sdk/standalone/src/main.cc](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc) | 在初始化处把 `eepnet_config` 与 `sizeof(eepnet_config)` 传给 `eeptpu_init`。 |

## 4. 核心概念与源码讲解

### 4.1 配置数组总体布局：从 bin 元数据到 C 数组

#### 4.1.1 概念说明

`eepnet_config[]` 是一段「自描述」的字节流：它既是数据，又自带结构说明。它的存在解决了裸机侧两个问题：

1. **没有运行库**：裸机无法在运行时解析 `*.pub.bin` 内部的复杂结构，所以把「网络需要几段 DDR 内存、每段多大、输入输出张量的形状、预处理系数」全部摊平成一个线性数组，编译期固化进程序。
2. **地址可重定位**：数组里存的是「相对偏移 `ofs`」，运行时再加上 `mem_base`（`0x31000000`）得到绝对地址。这样同一份 bin 即使被放到 DDR 不同位置也能用。

这个数组由 `eepBinCvt` 工具自动生成，文件头明确标注了来源与类型：

[sdk/standalone/src/net_data/eepnet.h:4-7](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/net_data/eepnet.h#L4-L7) 标注「由 eepBinCvt(v2.1.0) 生成，Public bin」，并声明 `eepnet_config[]` 数组——这两行注释是判断数组语义的第一手依据，「Public bin」对应 `bin_type=2`。

#### 4.1.2 核心流程

`eeptpu_sa.cpp` 顶部有一段格式注释，给出了数组的「设计意图」字段顺序：

[sdk/standalone/src/eeptpu/eeptpu_sa.cpp:106-115](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L106-L115) 描述了 `interface / mem_base / tpureg_addr / reg_size / bin_type / mem_cnt / base_ofs… / mem_size / CntOut / DataOut… / DataAlg_addr / DataIn / mean_count / mean_list / norm_list` 的排列——这是理解整个数组的总纲。

按注释，字段顺序可概括为五段：

```text
[1] 头部 4 字   : interface, mem_base, tpureg_addr, reg_size
[2] bin 元信息  : bin_type, mem_cnt, base_ofs0..N, mem_size, CntOut
[3] 输出张量表  : 每项 = ofs, shape[0..3], exp   （共 CntOut 项）
[4] 算法表 + 输入: DataAlg_addr, DataIn(ofs, shape[0..3], exp)
[5] 预处理参数  : mean_count, mean_list(fp32*N), norm_list(fp32*N)
```

需要特别提醒：这段注释写的是「for lib enc/pub」的**通用格式**，而裸机 `eeptpu_init` 的**实际读取顺序**与之有几处出入（多了 `cnt_threads`、跳过一个保留字）。本讲以代码为唯一真相，4.2 会逐一指出差异。

#### 4.1.3 源码精读

数组的实际内容是一长串十六进制字节，按每行 16 字节排列：

[ sdk/standalone/src/net_data/eepnet.h:8-17 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/net_data/eepnet.h#L8-L17) 共 10 行 × 16 字节 = **160 字节 = 40 个 32 位整数**。这 40 个 int 就是本讲要逐个解码的对象。

调用处把整个数组连同长度喂给 `eeptpu_init`：

[ sdk/standalone/src/main.cc:288-289 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L288-L289) 注释「Initial EEP TPU Config information from array eepnet_config」，调用 `eepsa.eeptpu_init((unsigned char *)0x10000000, 0, eepnet_config, sizeof(eepnet_config))`——`config` 参数就是数组首地址，`cfglen` 就是 `sizeof(eepnet_config)=160`。

头部 4 个字段在裸机里被**直接跳过并覆盖**：

[ sdk/standalone/src/eeptpu/eeptpu_sa.cpp:116-125 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L116-L125) 把 `config` 强转为 `unsigned int* pcfg`，先检查 `cfglen < 8*4`（即 32 字节）则直接返回 `-1`；随后 `pcfg+=4` 跳过头部 4 个 int，改用 `config.h` 里的宏 `EEPTPU_MEM_BASE_ADDR`/`EEPTPU_REG_BASE_ADDR` 覆盖 `mem_base` 与 `tpureg_addr`。原因是裸机地址由硬件设计定死，不能让 bin 里的值左右。

[ sdk/standalone/src/config.h:25-26 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L25-L26) 定义 `EEPTPU_MEM_BASE_ADDR=0x31000000`、`EEPTPU_REG_BASE_ADDR=0xA0000000`——注意数组里 int2 的值是 `0x43C00000`（arm32 寄存器基址），但裸机在 arm64 上运行，所以这个值被丢弃，以宏为准。这正是 u2-l4 提到的「arm32/arm64 地址不同」在配置数组里的遗留痕迹。

#### 4.1.4 代码实践

**实践目标**：确认数组的总尺寸与「整数个数」，为后续逐字段解码建立坐标。

**操作步骤**：

1. 打开 `sdk/standalone/src/net_data/eepnet.h`，数一数 `eepnet_config[]` 里有多少个字节（每行 16 字节，共 10 行）。
2. 在 `main.cc:289` 处确认 `sizeof(eepnet_config)` 被当作 `cfglen` 传入。
3. 在 `eeptpu_sa.cpp:117` 处确认最小长度校验是 `8*4`（32 字节），思考：为什么最小校验只看前 8 个 int 而不是全部 40 个？

**需要观察的现象**：数组共 160 字节 = 40 个 int32；最小校验 `8*4=32` 字节只够覆盖「头部 4 + bin_type/mem_cnt/2 个 base_ofs」这 8 个字段，说明函数对后续变长部分（输出表、mean/norm）的长度信任调用方传入的 `cfglen`，没有逐段再校验。

**预期结果**：`sizeof(eepnet_config) == 160`。若你手动解码完整数组后所有字段相加也恰为 40 个 int，则证明你的解码与代码读取完全对齐（4.4 综合实践会验证）。

#### 4.1.5 小练习与答案

**练习 1**：数组里 int2 的值是 `0x43C00000`，但裸机运行时实际用的寄存器基址是 `0xA0000000`。这两个值为什么不一致？以哪个为准？

**参考答案**：`0x43C00000` 是 arm32（Zynq-7000）家族的 PS 物理地址，是 bin 生成时写入的「默认值」；而本裸机工程跑在 arm64（ZynqMP）上，寄存器基址应为 `0xA0000000`。代码在 `eeptpu_sa.cpp:122-124` 用 `pcfg+=4` 跳过头部 4 字段，并以 `config.h` 的 `EEPTPU_REG_BASE_ADDR` 覆盖，所以**以宏为准**，数组里的值被忽略。

**练习 2**：为什么配置数组里存的是「偏移 `ofs`」而不是「绝对地址」？

**参考答案**：偏移加上运行时的 `mem_base` 才得到绝对地址（`hwaddr = ofs + mem_base`）。这样同一份 bin/数组可以被重定位到 DDR 的不同基址，只需改 `EEPTPU_MEM_BASE_ADDR` 一个宏即可，增强了部署灵活性。

### 4.2 eeptpu_init 的解析逻辑：指针游走法

#### 4.2.1 概念说明

`eeptpu_init` 解析数组的方式非常朴素：把数组首地址当成 `unsigned int*` 指针 `pcfg`，然后**每读一个字段就把指针前移一格**（`*pcfg++`）。这叫「指针游走法」——没有结构体偏移计算，没有反序列化框架，就是一个个 int 顺序读。

这种写法的优点是紧凑、零依赖，适合裸机；缺点是**字段顺序就是协议本身**，少读或多读一格都会让后面所有字段错位。因此理解这段代码等于理解一份「二进制协议」。

`bin_type` 是这份协议里的「分支开关」：`enc=1` 时读 2 个基址，`pub=2` 时读 4 个基址。免费版交付的是 Public bin，所以本数组 `bin_type=2`。

#### 4.2.2 核心流程

指针游走的完整路径（以本数组 `bin_type=2`、`cnt_out=2`、`in_ch=3` 为例）：

```text
pcfg 起始
 ├─ pcfg+=4            // 跳过头部 4 字（interface/mem_base/tpureg_addr/reg_size）
 ├─ bin_type  = *pcfg++   // =2 (pub)
 ├─ mem_cnt   = *pcfg++   // =4
 ├─ hwbase0   = *pcfg++ + mem_base   // 4 个基址偏移 (par/in/tmp/out)
 ├─ hwbase1   = *pcfg++ + mem_base
 ├─ hwbase2   = *pcfg++ + mem_base
 ├─ hwbase3   = *pcfg++ + mem_base
 ├─ memsize   = *pcfg++
 ├─ cnt_out   = *pcfg++   // =2
 ├─ for i in [0,cnt_out):  // 每个输出 6 字: ofs,shape[0..3],exp
 │     addr_out[i] = {ofs+mem_base, shape[4], exp}
 ├─ cnt_threads = *pcfg++        // 注释里没有的字段
 ├─ addr_alg    = *pcfg++ + mem_base
 ├─ *pcfg++                      // 跳过 1 个保留字（语义待确认）
 ├─ addr_in     = {ofs+mem_base, shape[4], exp}   // 6 字
 ├─ in_ch       = *pcfg++        // =3
 ├─ mean[0..in_ch) = *(float*)pcfg++   // 3 个 fp32
 └─ norm[0..in_ch) = *(float*)pcfg++   // 3 个 fp32
```

把每一步消耗的 int 数加起来：4+1+1+4+1+1+(2×6)+1+1+1+6+1+3+3 = **40**，恰好等于 `sizeof(eepnet_config)/4`。这说明指针走完整个数组正好不剩一字节，是验证解码正确性的有力旁证。

#### 4.2.3 源码精读

分支读取基址的关键代码：

[ sdk/standalone/src/eeptpu/eeptpu_sa.cpp:127-140 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L127-L140) 先读 `bin_type` 与 `mem_cnt`；若 `bin_type==1`（enc）只读 `hwbase0/hwbase1` 两个偏移，若 `bin_type==2`（pub）则读 `hwbase0..3` 四个偏移，每个都 `+mem_base` 得到绝对地址。注意：代码**按 `bin_type` 决定读几个基址，而不是按 `mem_cnt`**——注释里的 `base_ofs0..N` 容易让人误以为个数由 `mem_cnt` 控制，实际并非如此。

`mem_cnt` 字段的去向：

[ sdk/standalone/src/eeptpu/eeptpu_sa.h:91-94 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h#L91-L94) 把 `bin_type`、`mem_cnt`、`hwbase2`、`hwbase3` 都存为类成员。`mem_cnt` 被保存但在 `eeptpu_init` 内并未用于循环上界，更多是记录「该 bin 一共占几段内存」的元信息，供其他逻辑参考。

读取「算法表地址 + 一个保留字」的代码：

[ sdk/standalone/src/eeptpu/eeptpu_sa.cpp:158-160 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L158-L160) 这里是注释与代码差异最大的地方：先读 `cnt_threads`（注释未列出），再读 `addr_alg`（对应注释的 `DataAlg_addr`），然后 `*pcfg++` **无条件跳过一个 int**。这个被跳过的字段语义在源码中没有注释说明（**待确认**），从位置推测可能是 lib 格式里遗留的「输入个数 cnt_in」或保留字；裸机固定单输入，故直接跳过。这一点务必以代码为准，不要被顶部注释误导。

解析完成后，把恢复出来的地址打印并配置给底层接口：

[ sdk/standalone/src/eeptpu/eeptpu_sa.cpp:176-187 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L176-L187) 打印 `mem_base`/`tpureg_addr`/`hwbase0..3`/`memsize`/`addr_alg`，并把 `mem_base` 与 `tpureg_addr` 写入 `eepif`（底层 AXI 接口对象），随后读 TPU 硬件版本寄存器 `0x44` 验证链路。这些打印是上板调试时核对解码是否正确的第一手观测点。

#### 4.2.4 代码实践

**实践目标**：用「指针步数」推算每个字段的字节偏移，验证与数组实际字节对齐。

**操作步骤**：

1. 按上面核心流程的步数表，计算 `bin_type` 字段的字节偏移：前 4 字跳过 → `bin_type` 位于第 5 个 int，偏移 `4×4 = 16 = 0x10` 字节。
2. 打开 `eepnet.h`，定位偏移 `0x10` 处的字节：第 2 行（0x10 起）开头是 `0x02,0x00,0x00,0x00`，即 `bin_type=2`，与推算一致。
3. 同理推算 `cnt_out` 的偏移：4(头)+1(bin_type)+1(mem_cnt)+4(base_ofs)+1 = 12 个 int → 偏移 `12×4 = 48 = 0x2C`。读 `eepnet.h` 偏移 0x2C 处（第 3 行第 4 个 int）应为 `0x02,0x00,0x00,0x00`，即 `cnt_out=2`。

**需要观察的现象**：每个推算偏移处读出的值都与字段语义吻合。

**预期结果**：`bin_type@0x10=2`、`cnt_out@0x2C=2`、`in_ch@0x84=3`（按步数推算 in_ch 是第 34 个 int，偏移 `33×4=132=0x84`）。若三处都对得上，说明你对指针游走的理解正确。

**待本地验证**：以上偏移推算基于静态阅读，建议在板上运行 demo 时观察 `eeptpu_init` 打印的 `output cnt`、`out[i]: hwaddr/shape`、`in: hwaddr/shape` 等行，与你的推算逐一比对。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `bin_type` 误读成 `1`（enc），后续所有字段会怎样？

**参考答案**：`bin_type==1` 分支只读 2 个基址（`hwbase0/hwbase1`），比 `pub` 分支少读 2 个 int。于是从 `memsize` 开始，后面所有字段都会**向前错位 2 个 int（8 字节）**，读出的 shape、exp、mean/norm 全部变成无意义的值，推理必然失败。这正是「字段顺序即协议」的脆弱之处。

**练习 2**：代码里 `*pcfg++` 跳过的那个保留字，为什么裸机可以直接跳过而不用担心？

**参考答案**：裸机固定单输入网络（`addr_in` 是单个结构体而非 vector），输入个数恒为 1，无需读取该字段即可知道只有一份输入；同时 `eepBinCvt` 生成的数组里该字段确实占 4 字节，跳过它能让指针重新对齐到后续的 `DataIn` 字段。这种「跳过已知占位」是处理版本化二进制格式的常见手法。

### 4.3 输出/输入 shape 与 exp：恢复张量形状

#### 4.3.1 概念说明

解析配置数组的终极目的之一，是恢复出**输入张量**和**输出张量**的形状与位置，这样程序才知道往哪里写输入、从哪里读输出、每个值该如何反量化。

每个张量在数组里由 `st_hwaddr_info` 描述，共 6 个 int：

[ sdk/standalone/src/eeptpu/eeptpu_sa.h:30-35 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h#L30-L35) 定义 `st_hwaddr_info{ hwaddr; shape[4]; exp; }`——分别是绝对硬件地址、NCHW 四维形状、定点指数。

- **输出张量表**：数量由 `cnt_out` 决定，存入 `vector<st_hwaddr_info> addr_out`。
- **输入张量**：单个，存入 `addr_in`。
- **`exp`**：定点指数。输出侧 `exp=8` 表示读出的 int16 要除以 \(2^8=256\) 才是浮点；输入侧 `exp=12` 表示输入被乘以 \(2^{12}=4096\) 后存进硬件。

#### 4.3.2 核心流程

输出表的读取是一个循环，每轮吃 6 个 int：

```text
for i in [0, cnt_out):
    info.hwaddr   = ofs + mem_base      // ofs = *pcfg++
    info.shape[0] = *pcfg++             // N
    info.shape[1] = *pcfg++             // C
    info.shape[2] = *pcfg++             // H
    info.shape[3] = *pcfg++             // W
    info.exp      = *pcfg++             // 定点指数
    addr_out.push_back(info)
```

输入张量紧接着读取，结构完全相同（也是 6 个 int）。决定输入分辨率的是 `addr_in.shape[2]`（H）与 `addr_in.shape[3]`（W）；决定输入通道数的是 `addr_in.shape[1]`（C）。

#### 4.3.3 源码精读

输出表循环：

[ sdk/standalone/src/eeptpu/eeptpu_sa.cpp:146-157 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L146-L157) 对每个输出，依次读 `ofs(+mem_base)`、`shape[0..3]`、`exp`，并 `printf` 出 `hwaddr` 与四维 shape，最后 `push_back` 进 `addr_out`。打印行是上板核对输出形状的最直接证据。

输入张量读取：

[ sdk/standalone/src/eeptpu/eeptpu_sa.cpp:161-169 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L161-L169) 读 `addr_in` 的 `hwaddr/shape[0..3]/exp` 并打印。结合本数组实际值，可解码出输入为 `[1, 3, 416, 416]`、`exp=12`——这正是 yolov4-tiny 的标准输入。

shape 与 exp 的实际使用发生在 `read_forward_result`：

[ sdk/standalone/src/eeptpu/eeptpu_sa.cpp:364-384 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L364-L384) 遍历 `addr_out`，用 `shape[1..3]` 调 `epmat_get_size` 算出 epmat 字节数，`mem_read` 把定点数据从 `hwaddr` 读回，再调 `epmat2nmat(shape[1], shape[2], shape[3], epmat, exp)` 反量化成 `ncnn::Mat`。可见配置数组解析出的 shape/exp 直接决定了输出读取的字节数与反量化除数。

把字节解码成表格（小端 int32），输出与输入张量部分如下：

| int 序号 | 字节偏移 | 字段 | 原始值(u32) | 解析值 | 含义 |
| --- | --- | --- | --- | --- | --- |
| 12 | 0x30 | out[0].ofs | 0x010F44C0 | hwaddr=0x320F44C0 | 输出0 偏移 |
| 13 | 0x34 | out[0].shape[0] | 1 | N=1 | batch |
| 14 | 0x38 | out[0].shape[1] | 255 | C=255 | 通道（3×85） |
| 15 | 0x3C | out[0].shape[2] | 13 | H=13 | 高 |
| 16 | 0x40 | out[0].shape[3] | 13 | W=13 | 宽 |
| 17 | 0x44 | out[0].exp | 8 | \(2^8=256\) | 定点指数 |
| 18 | 0x48 | out[1].ofs | 0x011096C0 | hwaddr=0x321096C0 | 输出1 偏移 |
| 19 | 0x4C | out[1].shape[0] | 1 | N=1 | batch |
| 20 | 0x50 | out[1].shape[1] | 255 | C=255 | 通道 |
| 21 | 0x54 | out[1].shape[2] | 26 | H=26 | 高 |
| 22 | 0x58 | out[1].shape[3] | 26 | W=26 | 宽 |
| 23 | 0x5C | out[1].exp | 8 | \(2^8=256\) | 定点指数 |
| 27 | 0x6C | in.ofs | 0x00BAC4C0 | hwaddr=0x31BAC4C0 | 输入偏移 |
| 28 | 0x70 | in.shape[0] | 1 | N=1 | batch |
| 29 | 0x74 | in.shape[1] | 3 | C=3 | **通道数** |
| 30 | 0x78 | in.shape[2] | 416 | H=416 | **分辨率-高** |
| 31 | 0x7C | in.shape[3] | 416 | W=416 | **分辨率-宽** |
| 32 | 0x80 | in.exp | 12 | \(2^{12}=4096\) | 定点指数 |

> 说明：int24–26（`cnt_threads`/`addr_alg`/保留字）夹在输出表与输入之间，见 4.2。

#### 4.3.4 代码实践

**实践目标**：从十六进制字节手动解码出两个输出张量的形状，并对照 yolov4-tiny 的网络结构验证合理性。

**操作步骤**：

1. 在 `eepnet.h` 中定位偏移 `0x30`（int12，输出0 起点），按小端读 6 个 int，得到 `[1, 255, 13, 13]`、`exp=8`。
2. 继续从偏移 `0x48`（int18，输出1 起点）读 6 个 int，得到 `[1, 255, 26, 26]`、`exp=8`。
3. 思考：416 输入下，`13×13` 与 `26×26` 分别对应下采样 32 倍与 16 倍的两个检测分支；`255 = 3 × (5 + 80)`，即 3 个 anchor ×（4 个框坐标 + 1 个置信度 + 80 个 COCO 类别）。

**需要观察的现象**：两个输出形状正好是 yolov4-tiny 的两个 yolo 分支，与 `config.h` 里 `NET_TYPE = NetType_Object_Detect` 一致。

**预期结果**：输出0 = `[1,255,13,13]`，输出1 = `[1,255,26,26]`，二者 `exp` 均为 8。若解码出别的值，说明 int 偏移算错。

#### 4.3.5 小练习与答案

**练习 1**：如果要把网络输入分辨率从 416 改成 320，配置数组里哪些字段会变？

**参考答案**：`addr_in.shape[2]`（H）与 `addr_in.shape[3]`（W）会从 416 变成 320；同时两个输出分支的 H/W 也会随之改变（320/32=10、320/16=20，即输出变成 `[1,255,10,10]` 与 `[1,255,20,20]`）。注意这些值由 `eepBinCvt` 重新生成，不应手改数组——应改模型 cfg 后重新编译。

**练习 2**：输出 `exp=8` 意味着反量化时除以 \(2^8=256\)。如果读出某个 int16 原始值为 `1024`，它对应的浮点值是多少？

**参考答案**：\(1024 / 256 = 4.0\)。

### 4.4 mean/norm 列表：烤进 bin 的预处理参数

#### 4.4.1 概念说明

`mean` 与 `norm` 是图像预处理的两个系数向量，每个输入通道一个值。标准预处理公式为：

\[
x_{\text{norm}} = (x_{\text{pixel}} - \text{mean}) \times \text{norm}
\]

在 u3-l1 里我们看到 `--mean`/`--norm` 是编译器参数，这些系数被「烤进」bin，最终落到 `eepnet_config[]` 末尾，使裸机程序无需额外配置文件就能复原预处理。

对 darknet yolo 而言，标准做法是 `mean=0`、`norm=1/255`（即直接 `pixel/255` 归一化到 [0,1]）。本节数据正好印证这一点。

#### 4.4.2 核心流程

mean/norm 的读取是全数组里唯一涉及 **float**（而非 int）的部分：

```text
in_ch = *pcfg++                       // 通道数 = mean/norm 的个数
for i in [0, in_ch): mean[i] = *(float*)pcfg++   // in_ch 个 fp32
for i in [0, in_ch): norm[i] = *(float*)pcfg++   // in_ch 个 fp32
```

关键是 `*(float*)pcfg`：把同一个 32 位字「重新解释」为 IEEE 754 单精度浮点。字节布局不变，解读方式从 int 切换到 float。

#### 4.4.3 源码精读

mean/norm 读取代码：

[ sdk/standalone/src/eeptpu/eeptpu_sa.cpp:170-172 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L170-L172) 先读 `in_ch`（即 `mean_count`），清空 `mean`/`norm` 两个 vector，然后分别读 `in_ch` 个 float 进 `mean` 与 `norm`。`in_ch` 与输入通道数 `addr_in.shape[1]` 一致（本数组均为 3）。

类成员定义：

[ sdk/standalone/src/eeptpu/eeptpu_sa.h:87-89 ](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h#L87-L89) `addr_in`、`mean`、`norm` 均为类成员——解析后供 `get_input_data`（u4-l4）做预处理时使用。

字节解码成表格（末尾 8 个 int，偏移 0x84–0x9C）：

| int 序号 | 字节偏移 | 字段 | 原始值(u32) | float 值 | 含义 |
| --- | --- | --- | --- | --- | --- |
| 33 | 0x84 | in_ch | 0x00000003 | 3 | 通道数 |
| 34 | 0x88 | mean[0] | 0x00000000 | 0.0 | R 通道均值 |
| 35 | 0x8C | mean[1] | 0x00000000 | 0.0 | G 通道均值 |
| 36 | 0x90 | mean[2] | 0x00000000 | 0.0 | B 通道均值 |
| 37 | 0x94 | norm[0] | 0x3B808081 | 1/255 ≈ 0.003922 | R 通道归一化 |
| 38 | 0x98 | norm[1] | 0x3B808081 | 1/255 ≈ 0.003922 | G 通道归一化 |
| 39 | 0x9C | norm[2] | 0x3B808081 | 1/255 ≈ 0.003922 | B 通道归一化 |

校验 `0x3B808081`：符号位 0，阶码 `0x77=119`（偏移后 \(119-127=-8\)），尾数 \(1 + 0x008081/2^{23} \approx 1.003922\)，故

\[
\text{norm} = 1.003922 \times 2^{-8} = \frac{1.003922}{256} \approx 0.0039216 = \frac{1}{255}
\]

正好是 darknet 的 `1/255` 归一化系数。三个 mean 全 0、三个 norm 全 `1/255`，与 yolov4-tiny 的标准预处理完全吻合。

数组在 `norm[2]` 之后正好结束，总长 `0xA0 = 160` 字节，与 4.2 的步数核算一致。

#### 4.4.4 代码实践

**实践目标**：亲手把一段 4 字节十六进制解释成 float，验证 `0x3B808081 = 1/255`。

**操作步骤**：

1. 取 `eepnet.h` 偏移 `0x94` 处的 4 字节：`0x81,0x80,0x80,0x3B`（小端）→ u32 = `0x3B808081`。
2. 拆位：最高位 `0`（正数），后 8 位 `0x77=119`（阶码），低 23 位 `0x008081`（尾数小数部分）。
3. 计算：\(1 + 32897/8388608 \approx 1.003922\)，再乘 \(2^{-8}\)，得 `0.0039216 ≈ 1/255`。
4. （可选）写一段最小 C 程序验证：

   ```c
   // 示例代码：把 4 字节重解释为 float
   unsigned char b[4] = {0x81,0x80,0x80,0x3B};
   float f = *(float*)b;
   printf("%g\n", f);   // 期望输出 0.0039216
   ```

**需要观察的现象**：程序输出约 `0.0039216`，与 `1/255` 一致。

**预期结果**：`0x3B808081` 解码为 `0.0039216`（即 `1/255`）。三个 norm 值相同，三个 mean 值为 `0.0`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `mean` 和 `norm` 要按通道数 `in_ch` 各存一份，而不是只存一个标量？

**参考答案**：不同通道可能需要不同的均值/归一化（例如某些模型用 ImageNet 的 per-channel mean `[0.485, 0.456, 0.406]`）。按通道存最通用；yolo 恰好三通道相同，但格式必须支持 per-channel。

**练习 2**：如果编译时把 `--norm` 改成 `1/128`，配置数组里哪个字段会变？变成什么？

**参考答案**：`norm[0..2]` 三个 float 会变。`1/128 = 0.0078125`，其 IEEE 754 编码为 `0x3BC00000`（阶码 `0x78=120` 即 \(2^{-7}\)，尾数 1.0），所以数组偏移 0x94/0x98/0x9C 处的字节会变成 `0x00,0x00,0xC0,0x3B`。

## 5. 综合实践

**任务**：根据 `eeptpu_sa.cpp` 顶部的格式注释与实际代码，画出 `eepnet_config[]` 数组各字段在内存中的完整排列顺序图，并标注哪些字段决定**输入分辨率**与**输入通道数**。

**参考答案（内存布局图）**：

```text
偏移      int#  字段              值(u32)        → 解析
--------  ----  ----------------  -------------  --------------------------------
0x00      0     interface         0x00000001       (裸机跳过)
0x04      1     mem_base          0x00000000       (被 EEPTPU_MEM_BASE_ADDR 覆盖)
0x08      2     tpureg_addr       0x43C00000       (被 EEPTPU_REG_BASE_ADDR 覆盖)
0x0C      3     reg_size          0x00000000       (忽略)
0x10      4     bin_type          0x00000002       pub=2
0x14      5     mem_cnt           0x00000004       4 段内存
0x18      6     base_ofs0(par)    0x00000000       hwbase0
0x1C      7     base_ofs1(in)     0x00BAC4C0       hwbase1
0x20      8     base_ofs2(tmp)    0x0115DEC0       hwbase2
0x24      9     base_ofs3(out)    0x010F44C0       hwbase3
0x28     10     memsize           0x01BCDE00       权重总大小
0x2C     11     cnt_out           0x00000002       2 个输出
0x30     12     out[0].ofs        0x010F44C0       输出0 地址
0x34     13     out[0].shape[0]   1                N
0x38     14     out[0].shape[1]   255              C
0x3C     15     out[0].shape[2]   13               H
0x40     16     out[0].shape[3]   13               W
0x44     17     out[0].exp        8                2^8
0x48     18     out[1].ofs        0x011096C0       输出1 地址
0x4C     19     out[1].shape[0]   1                N
0x50     20     out[1].shape[1]   255              C
0x54     21     out[1].shape[2]   26               H
0x58     22     out[1].shape[3]   26               W
0x5C     23     out[1].exp        8                2^8
0x60     24     cnt_threads       0x00000001       线程数(注释未列)
0x64     25     addr_alg.ofs      0x00B8C400       算法表地址
0x68     26     (保留/skip)       0x00000001       代码跳过(语义待确认)
0x6C     27     in.ofs            0x00BAC4C0       输入地址
0x70     28     in.shape[0]       1                N
0x74     29     in.shape[1]       3              ★ 决定输入通道数 C=3
0x78     30     in.shape[2]       416            ★ 决定输入分辨率-高 H=416
0x7C     31     in.shape[3]       416            ★ 决定输入分辨率-宽 W=416
0x80     32     in.exp            12               2^12=4096
0x84     33     in_ch             0x00000003       mean/norm 个数=3
0x88     34     mean[0]           0x00000000       0.0
0x8C     35     mean[1]           0x00000000       0.0
0x90     36     mean[2]           0x00000000       0.0
0x94     37     norm[0]           0x3B808081       1/255
0x98     38     norm[1]           0x3B808081       1/255
0x9C     39     norm[2]           0x3B808081       1/255
--------  ----  ----------------  -------------  --------------------------------
总长 0xA0 = 160 字节 = 40 个 int32，与 sizeof(eepnet_config) 一致
```

**结论**：决定输入分辨率的是 `in.shape[2]`（H=416）与 `in.shape[3]`（W=416）；决定输入通道数的是 `in.shape[1]`（C=3）与 `in_ch`（=3，二者必须一致）。读者可对照 `eeptpu_init` 的打印输出逐行核对此图。

## 6. 本讲小结

- `eepnet_config[]` 是 `eepBinCvt` 生成的 160 字节元数据数组，把网络的内存布局、输入输出形状、预处理系数全部摊平成线性 int 序列。
- `eeptpu_init` 用「指针游走法」逐字段解析：`pcfg+=4` 跳过头部并改用 `config.h` 宏覆盖地址，再依次读 `bin_type`、基址、输出表、算法表、输入、mean/norm。
- `bin_type` 是分支开关：`enc=1` 读 2 个基址，`pub=2` 读 4 个基址；本数组为 `pub=2`，对应 par/in/tmp/out 四段内存。
- 每个张量由 `st_hwaddr_info{hwaddr, shape[4], exp}` 描述；输入 `[1,3,416,416]`、exp=12，两个输出 `[1,255,13,13]` 与 `[1,255,26,26]`、exp=8，正是 yolov4-tiny。
- mean/norm 以 IEEE 754 float 存储，本数组为 `mean=[0,0,0]`、`norm=[1/255,1/255,1/255]`，即 darknet 的 `x/255` 归一化。
- 顶部格式注释是「lib 通用格式」，与裸机实际读取有出入（多了 `cnt_threads`、跳过一个保留字），**务必以代码为准**。

## 7. 下一步学习建议

- **u4-l2（EEPTPU_SA 类与 TPU 寄存器协议）**：本讲只解析了配置数组；接下来看 `hwbase0..3` 与 `addr_alg` 如何被写进 TPU 的 `BASEADDR`/`ALGOADDR` 寄存器并启动推理。
- **u4-l4（裸机输入预处理与硬件输入格式）**：本讲得到的 `mean`/`norm`/`exp` 与输入 shape 会在 `get_input_data` 里被用来做 resize、归一化、定点化与 32 字节步长打包，是配置数组的直接消费者。
- **u5-l2（输出读取与 epmat→ncnn::Mat 转换）**：本讲的输出 shape 与 `exp` 在 `read_forward_result`/`epmat2nmat` 里决定读取字节数与反量化除数，可对照阅读。
- **延伸阅读**：用 `git show` 查看 `sdk/standalone/src/net_data/eepnet.h` 的提交历史，观察换网络后该数组如何变化，加深对「换模型即换 config」的理解。
