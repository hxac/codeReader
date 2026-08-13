# Kernel ELF 结构与 meta 信息

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚一个 Ascend C 算子「编译产物」长什么样：它是一个标准 ELF 文件，但带 Ascend 专属的段。
- 解释 `.ascend.meta` 段里的 **TLV（Type-Length-Value）** 元信息布局，并能对照 C++ 源码画出字节排列。
- 区分 `KERNEL_TYPE`、`CROSS_CORE_SYNC`、`MIX_TASK_RATION` 三个关键字段的含义，理解**融合编译（fusion compile）**为什么会同时产出 `_mix_aic` 与 `_mix_aiv` 两份 kernel。
- 理解 `.aicore_binary` 段的「外层 ELF 包内层 ELF」嵌套关系，以及 msobjdump 为何能自动拆解它。

本讲是「离线分析」工具链的第一站：cpu debug / npu check 关注算子「在 CPU 上跑得对不对」，而从本讲开始，我们把目光转向算子**编译落盘后的产物文件**——后续 u6-l2 会讲 Python 工具 `msobjdump` 如何把这些字段打印出来，本讲先打地基：产物本身的结构。

## 2. 前置知识

在进入本讲前，你需要具备以下认知（来自前置讲义 u1-l2，本讲不再重复）：

- **asc-tools 的目录分工**：`cpudebug/` 是 C++ 核心，`utils/msobjdump/` 是离线解析的 Python 工具。本讲会同时碰到两边——C++ 头文件 `kernel_elf_parser.h` 解释「字段在二进制里怎么排」，Python 工具 `msobjdump` 负责「把字段打印给人看」。
- **算子与 Kernel**：用 Ascend C 写的算子源码（如 `.asc` 文件）经编译器编译后，会得到一个可被 NPU 加载执行的目标文件，这个文件就是本讲的主角「Kernel ELF」。
- **CPU 域 / NPU 域**：同一份产物既可能被 cpudebug 在 CPU 上加载（用于孪生调试），也可能被 NPU 加载执行。无论哪种，加载方都需要先读懂产物里的元信息。

此外，本讲会用到一个通用计算机概念：

- **ELF（Executable and Linkable Format，可执行与可链接格式）**：Linux 下的可执行文件、`.o` 目标文件、`.so` 动态库都是 ELF。它由「文件头 + 一堆段（section）」组成。Ascend 的算子产物**复用了这套标准格式**，只是在里面塞了 Ascend 专属的段。这是本讲最关键的一个直觉：**Kernel ELF 不是全新格式，而是「标准 ELF + Ascend 私有段」**。

> 如果你完全没接触过 ELF，只需记住一句话：ELF 文件 = 一个文件头描述「我是谁、我的段表在哪」+ 一个段表描述「每个段叫什么名字、在文件里的偏移和大小」+ 各段的实际数据。本讲的 `.ascend.meta` 和 `.aicore_binary` 就是其中两个段。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它讲什么 |
| ---- | ---- | ---- |
| [docs/03_msobjdump.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/03_msobjdump.md) | msobjdump 工具官方说明 | 字段含义的权威字段表（VERSION/KERNEL_TYPE/CROSS_CORE_SYNC/MIX_TASK_RATION 等） |
| [examples/04_msobjdump/README.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/04_msobjdump/README.md) | MatmulLeakyRelu 融合编译样例 | 一份**真实**的 `--dump-elf` 输出，用来对照字段 |
| [cpudebug/include/kernel_elf_parser.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h) | C++ 运行期 ELF 解析器（头文件） | TLV 字节布局、`KernelType` 枚举、核占比解析的源码真相 |
| [cpudebug/include/stub_def.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/stub_def.h) | cpudebug 公共定义 | `KernelMode` 枚举（解析产物后最终映射到的运行模式） |
| [utils/msobjdump/msobjdump/utils.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/utils.py) | msobjdump 底层工具函数 | `.aicore_binary` 段的自动提取实现 |

> 一个提醒：本讲的 C++ 解析器（`kernel_elf_parser.h`）和 Python 工具（`msobjdump`）**读的是同一个产物文件，但关心不同的字段**——C++ 运行期只关心「这个 kernel 该按什么模式跑」，Python 工具则把所有字段都打印出来给人看。理解这个分工，能解释后面很多「为什么有些字段在 C++ 里找不到」的现象。

## 4. 核心概念与源码讲解

### 4.1 Kernel ELF 文件结构

#### 4.1.1 概念说明

算子源码（如 `add.asc`、`matmul_leakyrelu.asc`）经过 Ascend 编译器编译后，产出的是一个**目标文件**。和 Linux 下 `gcc -c` 产出的 `.o` 一样，它采用 ELF 格式，所以我们称它为 **Kernel ELF**。

Kernel ELF 与普通 ELF 的关系可以概括为一句话：

> **它是标准 ELF64，只是带两个 Ascend 专属段：`.ascend.meta.<kernel名>`（元信息）和 `.aicore_binary`（二进制负载）。**

这两个段的分工是：

- `.ascend.meta.<kernel名>`：存「描述信息」——这个 kernel 叫什么名字、版本号、运行在哪种核上、Cube/Vector 核占比多少、是否需要硬同步……这些是给加载方（runtime / 调测工具）看的「说明书」。
- `.aicore_binary`：存「真正的机器码与数据」——NPU 实际要搬上核去执行的内容。

为什么要把「说明书」和「机器码」分两个段？因为它们的使用者不同、读取时机也不同：元信息需要在「决定怎么调度这个 kernel」之前就读完，而机器码是在「真正要执行」时才加载。ELF 的段（section）机制天然适合这种隔离。

#### 4.1.2 核心流程：从外层 ELF 到内层 kernel

融合编译场景下，产物还有一层「套娃」结构：

```text
demo（外层 ELF，融合编译产物）
├── .aicore_binary 段  ──提取──►  demo.aicore.o（内层 ELF / 真正的 Kernel ELF）
│                                    ├── .ascend.meta.<kernel>_mix_aic  （Cube 侧 kernel 元信息）
│                                    ├── .ascend.meta.<kernel>_mix_aiv  （Vector 侧 kernel 元信息）
│                                    └── .text / .data ...（机器码）
└── 其他宿主侧段 ...
```

也就是说，融合编译产出的 `demo` 文件里，真正的算子 kernel 被打包在 `.aicore_binary` 这一个段里；把这个段单独抽出来（落盘成 `demo.aicore.o`），才得到一个「干净的」、可被 `.ascend.meta` 段描述的 Kernel ELF。msobjdump 在解析时会**自动**完成这步抽取，无需人工干预。

#### 4.1.3 源码精读

**① 证据一：产物确实是标准 ELF64。** 样例用 `msobjdump --verbose` 打出的 ELF Header 显示它就是一个普通的 64 位 ELF，只是 `Machine` 字段是标准 `readelf` 认不出的 Ascend 专属值 `0x1029`：

[examples/04_msobjdump/README.md:150-158](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/04_msobjdump/README.md#L150-L158) —— 展示 `Magic: 7f 45 4c 46 ...`（即 `\x7fELF`）、`Class: ELF64`、`Machine: <unknown>: 0x1029`。`7f 45 4c 46` 是所有 ELF 文件共有的魔数，证明这是 ELF；`0x1029`（十进制 4137）是 Ascend 的机器型号标识，标准工具不认识，所以显示 `<unknown>`。

**② 证据二：C++ 解析器用的就是系统 `<elf.h>` 里的标准类型。** `kernel_elf_parser.h` 直接 `#include <elf.h>`，解析时用的 `Elf64_Ehdr`（ELF 文件头）、`Elf64_Shdr`（段头）、`EI_CLASS`/`EI_DATA`（类别/字节序索引）全是标准 ELF 定义，没有自定义二进制格式：

[cpudebug/include/kernel_elf_parser.h:208-228](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h#L208-L228) —— `ParseElfHeader` 读取标准 `Elf64_Ehdr`，并根据 `EI_DATA` 选择大端/小端读法。它还显式拒绝 32 位 ELF（`Only support input elf is 64-bit format.`），印证产物是 ELF64。

**③ 证据三：`.aicore_binary` 的自动提取，靠的是 `llvm-objcopy` 把这个段单独抠出来。** Python 工具的实现只有一行核心命令：

[utils/msobjdump/msobjdump/utils.py:69-81](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/utils.py#L69-L81) —— `extract_aicore_binary_from_elf` 调用 `llvm-objcopy -O binary --only-section=.aicore_binary`，把外层 ELF 的 `.aicore_binary` 段原样落盘成 `<name>.aicore.o`。样例文档里「若该 ELF 中包含 `.aicore_binary` 段，msobjdump 会自动提取」说的就是这步：[examples/04_msobjdump/README.md:105](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/04_msobjdump/README.md#L105)。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认「Kernel ELF = 标准 ELF + Ascend 段」，并理解 `.aicore_binary` 的套娃关系。

**操作步骤**：

1. 打开 [examples/04_msobjdump/README.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/04_msobjdump/README.md)，找到 `--list-elf ./demo` 的输出（约 196–203 行）。
2. 对比 `--extract-elf ./demo` 的说明（约 205–211 行）。

**需要观察的现象**：

- `--list-elf` 输出 `ELF file 0: demo.aicore.o`，说明外层 `demo` 里**只列出一个**内层设备文件。
- `--extract-elf` 落盘得到的文件名正是 `demo.aicore.o`，与 `--list-elf` 列出的名字一致。

**预期结果**：你应当能说出——外层 `demo` 通过 `.aicore_binary` 段「内嵌」了 `demo.aicore.o` 这个真正的 Kernel ELF；`list` 是列出它的名字，`extract` 是把它写回磁盘。两者描述的是同一个内层对象。

> 说明：本实践为源码阅读型，未要求你本地实跑；若你已按 u1-l4 搭好环境，可在 `examples/04_msobjdump` 目录实跑上述命令验证（命令见该 README「编译运行」一节）。样例要求 CANN ≥ 9.0.0 且为 A2/A3/950PR 系列产品。

#### 4.1.5 小练习与答案

**练习 1**：为什么标准 `readelf` 会把 Kernel ELF 的 `Machine` 字段显示成 `<unknown>: 0x1029`？

**参考答案**：因为 `0x1029` 是 Ascend 私有的机器型号编号，并未登记在标准 ELF 的 `e_machine` 取值表里。`readelf` 只认识已登记的型号（如 x86、ARM），遇到未登记的就按 `<unknown>: <十六进制>` 显示。这反过来也证明：Kernel ELF 严格遵守 ELF 格式规范（所以 `readelf` 能解析它的结构），只是用一个专属的 `e_machine` 值表明「我是 Ascend 的」。

**练习 2**：如果直接对融合编译产物 `demo` 用 `msobjdump --dump-elf`，工具会不会因为「机器码被包在 `.aicore_binary` 里」而读不到 kernel 元信息？

**参考答案**：不会。msobjdump 在解析前会先检测 `.aicore_binary` 段是否存在，存在则调用 `extract_aicore_binary_from_elf`（即 `llvm-objcopy --only-section=.aicore_binary`）自动抽出内层 `*.aicore.o`，再对内层做元信息解析。这正是样例 README 强调的「自动提取，无需手工拆分」。

---

### 4.2 `.ascend.meta` 段的 TLV 元信息

#### 4.2.1 概念说明

`.ascend.meta.<kernel名>` 段里存的是这个 kernel 的「说明书」。这份说明书不是「键值对列表」那样松散，而是采用一种紧凑的二进制编码：**TLV（Type-Length-Value）**。

- **T（Type，类型）**：2 字节，说明「这条信息是什么字段」，例如 1 代表 KERNEL_TYPE、3 代表 MIX_TASK_RATION。
- **L（Length，长度）**：2 字节，说明「后面的 Value 占多少字节」。
- **V（Value，值）**：长度可变，真正的数据。

TLV 的好处是**自描述、可扩展、可跳过未知项**：解析方只要认识 Type 就读 Value，不认识就按 Length 跳过，不会因为新增字段而 break 老解析器。这正是一个跨编译器版本、跨产品的工具链很需要的设计。

一个 `.ascend.meta` 段就是一串首尾相接的 TLV：

\[
\text{section} = TLV_1 \,\|\, TLV_2 \,\|\, \dots \,\|\, TLV_n
\]

每一条 TLV 占用的字节数为：

\[
\text{sizeof}(TLV_i) = \underbrace{4}_{\text{Type(2)} + \text{Length(2)}} + \text{length}_i
\]

解析时只要「还有 ≥ 4 字节剩余」就继续读一条 TLV，用 `length` 推进游标，直到把整段读完。

#### 4.2.2 核心流程：TLV 遍历

`.ascend.meta` 段的解析循环可以用下面这段伪代码概括（对应源码 `GetKernelInfo`）：

```text
curData   = section 起始地址
remainLen = section 总大小 sh_size
while remainLen > sizeof(ElfTlvHead):           # 至少还能放下一个 TLV 头(4B)
    (type, length) = 读 curData 处的 ElfTlvHead
    if 4 + length > remainLen:                   # 长度越界 → 抛异常
        throw "Invalid TLV length"
    value = curData + 4 处长度为 length 的字节
    按 type 解释 value（例如 type=1 → kernelType）
    curData   += 4 + length
    remainLen -= 4 + length
```

注意三个细节：① 退出条件是 `remainLen > 4` 而非 `> 0`，因为凑不够一个 TLV 头就谈不上还有字段；② 每条 TLV 在读 Value 之前先用 `4 + length > remainLen` 防越界；③ Length 字段允许解析方**跳过不认识的 Type**——这正是 TLV 可扩展的来源。

#### 4.2.3 源码精读

**① TLV 头的结构定义。** Ascend 用一个 4 字节的 `ElfTlvHead` 把 Type/Length 钉死，紧跟其后的就是 Value：

[cpudebug/include/kernel_elf_parser.h:34-37](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h#L34-L37) —— `typedef struct { uint16_t type; uint16_t length; } ElfTlvHead;`，正是 T(2B)+L(2B)。后面紧邻的字节就是 Value。

**② 已知的 Type 编号。** C++ 运行期解析器只关心两个字段，它们的 Type 编号被定义为常量：

[cpudebug/include/kernel_elf_parser.h:25-26](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h#L25-L26) —— `FUNC_META_TYPE_KERNEL_TYPE = 1U;` 与 `FUNC_META_TYPE_MIX_TASK_RATION = 3U;`。也就是说在 TLV 的 Type 取值表里，**1 = KERNEL_TYPE，3 = MIX_TASK_RATION**。

> **关键不对称（重要）**：docs 字段表里还列了 `VERSION`、`CROSS_CORE_SYNC`、`DEBUG`、`DYNAMIC_PARAM` 等很多字段（见 [docs/03_msobjdump.md:29-42](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/03_msobjdump.md#L29-L42)），但 C++ 运行期解析器**只**认识 type=1 和 type=3 两条。其余字段（VERSION、CROSS_CORE_SYNC 等）是 Python 工具 `msobjdump` 用来「打印给人看」的，C++ 加载方用不到、于是根本不解析。这就是本讲反复强调的「C++ 与 Python 关心不同字段」的源头。

**③ TLV 遍历的真正实现。** `GetKernelInfo` 把 4.2.2 的伪代码落成了真实代码：

[cpudebug/include/kernel_elf_parser.h:305-342](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h#L305-L342) —— `while (remainLen > sizeof(ElfTlvHead))` 循环读 TLV；`type==KERNEL_TYPE` 时把 Value 当 `uint32_t` 读入 `kernelInfo.kernelType`；`type==MIX_TASK_RATION` 时把 Value 当两个 `uint16_t` 读入 `aicRation` 与 `aivRation`；最后 `curData += sizeof(ElfTlvHead) + tlvLength` 推进游标。注意它对 `type` 既不认识的 TLV **直接跳过**（既无 else 也无报错），完美体现了 TLV 的前向兼容。

**④ 段名本身携带 kernel 名字。** `.ascend.meta.<kernel名>` 这一段名的「后半截」就是 kernel 的符号名，解析器需要把它剥出来：

[cpudebug/include/kernel_elf_parser.h:369-383](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h#L369-L383) —— `ExtractKernelName` 先确认段名以 `.ascend.meta.` 开头，再去掉这个前缀；若结尾是 `_mix_aiv` 或 `_mix_aic` 则一并去掉。这解释了融合编译里「同一个算子有两个段」：`_mix_aic` 段和 `_mix_aiv` 段剥出来的 kernel 名其实是同一个。

#### 4.2.4 代码实践（阅读单元测试）

**实践目标**：用最小的合成数据，亲手验证 TLV 的字节排列与 `GetKernelInfo` 的解析结果。

**操作步骤**：

1. 打开单元测试 [tests/ut/testcase/tikcpp_case_common/test_kernel_elf_parser.cpp:228-253](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_case_common/test_kernel_elf_parser.cpp#L228-L253)（用例 `GetKernelInfo_MixTaskRation`）。
2. 对照该用例构造的 `tlvData` 字节数组，画出它的内存布局。

**需要观察的现象**：测试先放第一条 TLV（`type=KERNEL_TYPE(1)`，`length=4`，value=`K_TYPE_MIX_AIC_MAIN`），紧接着第二条 TLV（`type=MIX_TASK_RATION(3)`，`length=4`，value=`aicRation=1` 与 `aivRation=2`）。两段 TLV 在内存里首尾相接，没有间隔。

**预期结果**：画出如下布局（数字单位为字节）：

```text
偏移:  [0..3]  ElfTlvHead#1  type=1, length=4
       [4..7]  value#1       kernelType = K_TYPE_MIX_AIC_MAIN (uint32)
       [8..11] ElfTlvHead#2  type=3, length=4
       [12..13] value#2 高16位 aicRation = 1
       [14..15] value#2 低16位 aivRation = 2
```

`GetKernelInfo` 读完后断言 `info.kernelType == K_TYPE_MIX_AIC_MAIN`、`info.aicRation == 1`、`info.aivRation == 2` 全部成立——这正好与 4.2.2 的遍历逻辑一一对应。

> 说明：本实践为「读测试断言理解行为」型，未要求实跑；若本地已编译 UT，可执行该用例观察通过情况（构建入口见 build.sh 的测试相关参数，u9-l3 会详讲）。

#### 4.2.5 小练习与答案

**练习 1**：假设未来 Ascend 在 `.ascend.meta` 里新增了一个 Type=9 的字段，旧的 `GetKernelInfo` 还能正确解析吗？为什么？

**参考答案**：能。因为 `GetKernelInfo` 的循环用 `length` 推进游标，遇到 `type` 不是 1 或 3 的 TLV 时既不处理也不报错，只是按 `sizeof(ElfTlvHead) + tlvLength` 跳过。新增 Type=9 的字段会被旧解析器安全跳过，已有的 KERNEL_TYPE / MIX_TASK_RATION 仍能被正确读出。这就是 TLV「前向兼容」的价值。

**练习 2**：`ElfTlvHead` 用 `uint16_t` 存 length，理论上单条 Value 最大多大？这会限制什么？

**参考答案**：`uint16_t` 最大值是 \(2^{16}-1 = 65535 \) 字节，即单条 TLV 的 Value 至多约 64 KiB。由于 `.ascend.meta` 存的是「元信息」（名字、类型、占比、开关等小数据），这个上限完全够用；它限制的只是「单条元信息字段不能超过 64 KiB」，对真正的机器码（可能很大）毫无影响——机器码放在 `.aicore_binary` 段，不受 TLV 约束。

---

### 4.3 kernel 类型与核占比

#### 4.3.1 概念说明

`.ascend.meta` 里有两个字段最关键，它们共同回答一个问题：**这个 kernel 在 NPU 上到底要按什么方式调度执行？**

**第一个字段：`KERNEL_TYPE`（kernel 运行时核类型）。**

现代昇腾 NPU（如 Atlas A2/A3 系列）内部有两类计算核：

- **AIC（AI Cube，Cube 核 / 矩阵核）**：擅长矩阵乘等 Cube 类运算。
- **AIV（AI Vector，Vector 核 / 向量核）**：擅长逐元素、归约等 Vector 类运算。

`KERNEL_TYPE` 说明这个 kernel 是「跑在哪种核上的版本」。可能的取值有纯 AIC、纯 AIV、混合（MIX）等。

**第二个字段：`MIX_TASK_RATION`（Cube/Vector 核占比）。**

在融合编译里，一个算子（如 MatmulLeakyRelu）会被拆成 Cube 部分（矩阵乘）和 Vector 部分（LeakyReLU），两类核协同工作。`MIX_TASK_RATION` 形如 `[1:2]`，表示**每 1 颗 Cube 核搭配 2 颗 Vector 核**组成一个执行小组。这个配比直接影响编译产物里有几份 kernel、运行时分到几颗核。

**第三个字段（本讲实践重点之一）：`CROSS_CORE_SYNC`（硬同步 syncall 类型）。**

它取值 `USE_SYNC`（使用硬件同步）/ `NO_USE_SYNC`（不使用）。多核协同（尤其是 Cube 核与 Vector 核之间）需要同步屏障，`CROSS_CORE_SYNC` 声明这个 kernel 是否需要硬件级的跨核同步。该字段仅在 Atlas A2/A3 系列生效。

> 术语小结：**融合编译（fusion compile）** = 把多个算子（如 Matmul + LeakyReLU）编译进**同一份产物**、由 Cube/Vector 两类核**协同执行**的编译模式；与之对应，产物里会出现 `_mix_aic`（Cube 侧）与 `_mix_aiv`（Vector 侧）两份 kernel 元信息。

#### 4.3.2 核心流程：从产物字段到运行模式

C++ 运行期拿到 `.ascend.meta` 后，要把 `KERNEL_TYPE` + `MIX_TASK_RATION` 这两个字段翻译成一个最终的 `KernelMode`（运行模式）。映射规则（对应 `ToKernelMode`）：

```text
若 KERNEL_TYPE == MIX_AIC_MAIN:
    若 aicRation==1 且 aivRation==0  → AIC_MODE        （其实是纯 Cube）
    若 aicRation==1 且 aivRation==1  → MIX_AIC_1_1     （1 Cube + 1 Vector）
    若 aicRation==1 且 aivRation==2  → MIX_MODE        （1 Cube + 2 Vector，典型融合）
若 KERNEL_TYPE ∈ {AIC, AIC_ROLLBACK}                    → AIC_MODE
若 KERNEL_TYPE ∈ {AIV, AIV_ROLLBACK, MIX_AIV_MAIN}      → AIV_MODE
其它                                                      → MIX_MODE（兜底）
```

注意「主核」语义：融合编译里**以 Cube 核为主**（`MIX_AIC_MAIN`），由 Cube 侧 kernel 牵头调度、Vector 侧（`MIX_AIV_MAIN`）配合。所以样例里 `_mix_aic` 和 `_mix_aiv` 两条元信息的 `KERNEL_TYPE` 都打印成 `MIX_AIC_MAIN`，这正是「主从关系」的体现。

#### 4.3.3 源码精读

**① KERNEL_TYPE 的取值表。** `KernelType` 枚举列出了所有可能的核类型：

[cpudebug/include/kernel_elf_parser.h:39-49](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h#L39-L49) —— 从 `K_TYPE_INVALID=0` 到 `K_TYPE_AICORE=1`、`K_TYPE_AIC=2`、`K_TYPE_AIV=3`、`K_TYPE_MIX_AIC_MAIN=4`、`K_TYPE_MIX_AIV_MAIN=5`，以及两个回滚类型 `K_TYPE_AIC_ROLLBACK=6` / `K_TYPE_AIV_ROLLBACK=7`。这些编号就是写进 TLV Value 里的 `uint32_t`。

**② 解析结果存放处。** 三个字段被收进一个小结构体：

[cpudebug/include/kernel_elf_parser.h:51-55](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h#L51-L55) —— `struct ElfKernelInfo { uint32_t kernelType; uint16_t aicRation; uint16_t aivRation; }`。`kernelType` 来自 type=1 的 TLV，`aicRation`/`aivRation` 来自 type=3 的 TLV（见 4.2.3 的 ③）。

**③ 字段到运行模式的映射。** `ToKernelMode` 把上面 4.3.2 的规则落成代码：

[cpudebug/include/kernel_elf_parser.h:344-365](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h#L344-L365) —— 重点看 `K_TYPE_MIX_AIC_MAIN` 分支：`(aicRation==1, aivRation==2)` 返回 `KernelMode::MIX_MODE`（1 Cube + 2 Vector），这正是样例输出 `MIX_TASK_RATION: [1:2]` 所对应的运行模式。

**④ 最终的运行模式枚举。** 映射目标是 `KernelMode`：

[cpudebug/include/stub_def.h:170-175](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/stub_def.h#L170-L175) —— `enum class KernelMode { MIX_MODE=0, AIC_MODE, AIV_MODE, MIX_AIC_1_1 }`，共 4 种。这个枚举会被 cpudebug 在 fork 多核时用来决定怎么分组、怎么分配核号（u3-l1 的 `get_process_num()` 等机制会用到它）。

**⑤ 字段含义的权威文档。** docs 字段表给出了每个字段的「人话解释」，是理解取值语义的第一手资料：

[docs/03_msobjdump.md:36-38](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/03_msobjdump.md#L36-L38) —— 分别解释 `KERNEL_TYPE`（核类型）、`CROSS_CORE_SYNC`（硬同步类型，`USE_SYNC`/`NO_USE_SYNC`，仅 A2/A3 生效）、`MIX_TASK_RATION`（Cube/Vector 占比分配）。

**⑥ 一份真实产物输出。** 样例 `--dump-elf` 的输出把上述字段具体化了：

[examples/04_msobjdump/README.md:120-127](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/04_msobjdump/README.md#L120-L127) —— 对 `_mix_aic` 这一份 kernel：`KERNEL_TYPE: MIX_AIC_MAIN`、`CROSS_CORE_SYNC: USE_SYNC`、`MIX_TASK_RATION: [1:2]`。说明这是「以 Cube 为主、需要硬件同步、1 Cube 配 2 Vector」的融合 kernel。

#### 4.3.4 代码实践（对照输出与字段表）

**实践目标**：把「文档字段表」「样例真实输出」「C++ 映射规则」三者对上号。

**操作步骤**：

1. 读 [docs/03_msobjdump.md:36-38](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/03_msobjdump.md#L36-L38) 的字段定义。
2. 读 [examples/04_msobjdump/README.md:120-127](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/04_msobjdump/README.md#L120-L127) 的实际输出。
3. 对照 [cpudebug/include/kernel_elf_parser.h:349-356](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h#L349-L356) 的映射分支。

**需要观察的现象**：样例输出的 `MIX_TASK_RATION: [1:2]`，对应 `ToKernelMode` 中 `aicRation==1 && aivRation==2` 这一支，返回 `KernelMode::MIX_MODE`。即 cpudebug 在 CPU 上仿真这个 kernel 时，会按「1 个 Cube + 2 个 Vector」的拓扑来组织模拟核。

**预期结果**：你应当能复述这条因果链——产物里 `MIX_TASK_RATION: [1:2]` → TLV 解析得 `aicRation=1, aivRation=2` → `ToKernelMode` 命中第三支 → 运行模式 `MIX_MODE`。

> 说明：若本地有 A2/A3/950PR 环境并已编译样例，可在 `examples/04_msobjdump/build` 下执行 `msobjdump --dump-elf ./demo` 自行观察输出；否则以上「读三处对照」即为完整实践。待本地验证输出与 README 一致。

#### 4.3.5 小练习与答案

**练习 1**：样例输出里 `_mix_aic` 和 `_mix_aiv` 两份 kernel 的 `KERNEL_TYPE` 都显示为 `MIX_AIC_MAIN`，为什么 `_mix_aiv` 不是 `MIX_AIV_MAIN`？

**参考答案**：融合编译以 Cube 核为「主」、Vector 核为「从」，`MIX_AIC_MAIN` 标识的是「这一组融合 kernel 的主导方是 Cube」。两份 kernel 同属一个融合算子，由 Cube 侧牵头调度，所以它们在产物里共享同一组调度元信息（包括 `KERNEL_TYPE` 和 `MIX_TASK_RATION`），打印时都呈现为主核类型。`_mix_aiv` 这个后缀只表明「这是 Vector 侧的那份机器码」，与调度主导权无关。

**练习 2**：如果某个算子是纯 Vector 计算（不涉及矩阵乘），它的 `KERNEL_TYPE` 最可能是什么？经 `ToKernelMode` 会映射到哪个 `KernelMode`？

**参考答案**：最可能是 `K_TYPE_AIV`（或 `K_TYPE_AIV_ROLLBACK`）。经 `ToKernelMode` 的第三分支 `K_TYPE_AIV || K_TYPE_AIV_ROLLBACK || K_TYPE_MIX_AIV_MAIN → AIV_MODE`，映射到 `KernelMode::AIV_MODE`，即只会在 Vector 核上执行、不涉及 Cube 核。

---

## 5. 综合实践

**任务**：对照 docs/03 字段表，用一段话说明 `KERNEL_TYPE`、`CROSS_CORE_SYNC`、`MIX_TASK_RATION` 三个字段在**融合编译场景**下分别表达什么，并结合样例输出给出具体取值的解释。

**操作步骤**：

1. 打开字段表 [docs/03_msobjdump.md:29-42](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/03_msobjdump.md#L29-L42)，定位这三个字段。
2. 打开样例输出 [examples/04_msobjdump/README.md:114-128](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/04_msobjdump/README.md#L114-L128)。
3. 回到 C++ 源码，确认这三个字段里哪些会被运行期解析、哪些只用于打印：[cpudebug/include/kernel_elf_parser.h:25-26](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h#L25-L26)。
4. 写出你的三段式说明。

**参考答案要点**（可对照自检）：

- **`KERNEL_TYPE`**：表达「这份 kernel 运行在哪种核上」。融合编译以 Cube 为主，样例取值 `MIX_AIC_MAIN` 表示「这是一个以 Cube 核主导的融合 kernel」。它是 TLV 里 Type=1 的字段，**会被 C++ 运行期解析**，并经 `ToKernelMode` 决定运行模式。
- **`CROSS_CORE_SYNC`**：表达「是否需要硬件级跨核同步（syncall）」。融合编译中 Cube 核与 Vector 核需要协同，样例取值 `USE_SYNC` 表示「使用硬同步」。它**只在 A2/A3 系列生效**；并且它**不是** C++ 运行期解析器关心的字段（解析器只认 type=1 和 type=3），仅供 msobjdump 打印与上层 runtime 使用。
- **`MIX_TASK_RATION`**：表达「Cube 核与 Vector 核的数量配比」。样例取值 `[1:2]` 表示「每 1 颗 Cube 核搭配 2 颗 Vector 核」。它是 TLV 里 Type=3 的字段，**会被 C++ 运行期解析**为 `aicRation=1, aivRation=2`，并映射到 `KernelMode::MIX_MODE`。

**串联一句话**：融合编译把一个算子拆成 Cube/Vector 两份 kernel（`_mix_aic` / `_mix_aiv`），用 `KERNEL_TYPE` 声明主导方是 Cube、用 `MIX_TASK_RATION` 声明两者数量比、用 `CROSS_CORE_SYNC` 声明它们之间是否走硬件同步——三者共同描述了「这组融合 kernel 在多核上如何被调度」。

## 6. 本讲小结

- Kernel ELF **不是新格式**，而是标准 ELF64 + 两个 Ascend 专属段：`.ascend.meta.<kernel名>`（元信息）与 `.aicore_binary`（机器码负载）。
- 融合编译产物有「套娃」结构：外层 `demo` 的 `.aicore_binary` 段，抽出后就是内层 `demo.aicore.o`（真正的 Kernel ELF）；msobjdump 用 `llvm-objcopy --only-section=.aicore_binary` 自动完成抽取。
- `.ascend.meta` 段用 **TLV（Type-Length-Value）** 编码，`ElfTlvHead = {uint16_t type; uint16_t length;}` + Value；解析方靠 `length` 推进游标，遇到未知 Type 可安全跳过，天然前向兼容。
- **C++ 运行期解析器只关心两个 Type**：`KERNEL_TYPE`(type=1) 与 `MIX_TASK_RATION`(type=3)；`VERSION`、`CROSS_CORE_SYNC` 等其余字段是 Python 工具用来打印的，C++ 不解析——这是「同一产物、不同读者」的关键分工。
- `KERNEL_TYPE` 取值（`AIC`/`AIV`/`MIX_AIC_MAIN`/`MIX_AIV_MAIN`/回滚等）+ `MIX_TASK_RATION` 的 `[aic:aiv]` 配比，经 `ToKernelMode` 映射成最终运行模式 `KernelMode`（`MIX_MODE`/`AIC_MODE`/`AIV_MODE`/`MIX_AIC_1_1`）。
- 融合编译以 **Cube 核为主**：一个算子产出 `_mix_aic`（Cube 侧）与 `_mix_aiv`（Vector 侧）两份 kernel；样例 `MatmulLeakyRelu` 的 `[1:2]` 即「1 Cube + 2 Vector」。

## 7. 下一步学习建议

本讲只讲了「产物里有什么字段、字段长什么样」，还没有讲「Python 工具 `msobjdump` 如何把这些字段打印出来」。建议下一步学习 **u6-l2《ObjDump 解析流程实现》**，它将剖析 `msobjdump_main.py` 的 `ObjDump` 类：

- `--dump-elf` / `--extract-elf` / `--list-elf` 三种命令各自的实现差异；
- TLV 解析在 Python 侧的对应实现，以及 `--verbose` 如何控制打印范围（本讲看到的 ELF Header / Section Headers 全量信息只在 `--verbose` 下输出）。

此外，若你想了解这些产物**在 CPU 仿真时是如何被加载并影响多核 fork 的**，可回顾 u3-l1《多核 fork 执行模型》——本讲的 `KernelMode` 正是 u3-l1 中决定「分几个核、怎么分组」的输入之一。
