# 字节码写入器与二进制格式

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `.tilebc` 文件的整体二进制布局：magic number + 版本头 + 一串 section；
- 在源码中定位 `writeBytecode` 入口，讲清楚它如何「先校验、再编排各段写出」；
- 解释 `EncodingWriter` 的三种核心编码：变长整数（varint）、有符号变长整数（zigzag）、对齐（alignment）；
- 说出 `StringManager / TypeManager / ConstantManager / DebugInfoWriter / FunctionTableWriter` 各自的职责，以及它们为何采用「收集在前、落盘在后」的设计；
- 学会用十六进制工具查看自己生成的 `.tilebc`，并在文件中标注 magic、版本字节和各 section 的位置。

本讲只讲**写入端**（序列化）；读取端（反序列化）留待下一讲 u7-l3。

## 2. 前置知识

本讲假设你已经掌握 u7-l1 的内容，特别是：

- `cuda-tile-translate --mlir-to-cudatilebc` 这条命令把文本 MLIR 序列化成 `.tilebc`，最终收口到一个叫 `writeBytecode` 的函数；
- 字节码有版本概念，默认写「当前版」13.3，要追求广泛兼容则显式指定「兼容版」13.1，合法区间是 [13.1, 13.3]；
- MLIR 的基本对象：`ModuleOp`（模块）、`Operation`（操作）、`Type`（类型）、`Attribute`（属性）、`Value`（SSA 值）。本讲把它们「拍平」成字节。

另外有两个二进制基础概念需要先建立直觉：

- **varint（变长整数）**：用一个字节的低 7 位存数据，最高位（0x80）当「还有后续字节」的续位标志。这样小数字只要 1 字节，大数字才用多字节，比定长 8 字节省空间。protobuf 等很多二进制格式都用这套。
- **对齐（alignment）**：很多 CPU/GPU 读取数据时，地址按 4 或 8 字节对齐会更快。字节码格式在段内预留填充字节（padding），让后续数据落在对齐地址上，读取器可以直接 `memcpy` 而不必逐字节拼装。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lib/Bytecode/Writer/BytecodeWriter.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp) | 写入器全部实现：`EncodingWriter` 编码工具、各 Manager、各 section 写出函数、`writeBytecode` 入口。 |
| [include/cuda_tile/Bytecode/Writer/BytecodeWriter.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Writer/BytecodeWriter.h) | 对外只暴露一个函数 `writeBytecode(os, module, targetVersion)`。 |
| [lib/Bytecode/BytecodeEnums.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h) | section ID 枚举、对齐填充字节常量 `kAlignmentByte`、函数标志位 `FunctionFlags`、调试标签 `DebugTag`。 |
| [include/cuda_tile/Bytecode/Common/Version.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/Version.h) | `BytecodeVersion` 类型与各版本常量声明。 |
| [lib/Bytecode/Common/Version.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp) | 版本常量的具体取值（兼容版 13.1、当前版 13.3、最低版 13.1）。 |

> 说明：写入器里还会 `#include "Bytecode.inc"`、`"TypeBytecode.inc"`、`"StaticOpcodes.inc"`、`"AttrBytecode.inc"` 等由 `cuda-tile-tblgen` 生成的胶水代码（见 u8-l1）。本讲把它们当黑盒，只关注「生成的代码在写入流程里扮演什么角色」。

## 4. 核心概念与源码讲解

### 4.1 writeBytecode 入口：自包含校验与编排管线

#### 4.1.1 概念说明

`writeBytecode` 是整个序列化的总入口，它在头文件里只有一行声明，是写入端对外暴露的**唯一**公开函数：

[include/cuda_tile/Bytecode/Writer/BytecodeWriter.h:18-21](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Writer/BytecodeWriter.h#L18-L21) —— 把一个 `cuda_tile::ModuleOp` 写到输出流，可选地指定目标版本（默认 `kCurrentCompatibilityVersion`，即 13.1）。

它的核心职责不是「亲手写每一个字节」，而是**当指挥**：先做一次前置校验，再实例化一群 Manager（各管一张表），最后按固定顺序把 header 和各 section 依次落到输出流里。

#### 4.1.2 核心流程

`writeBytecode` 的执行顺序可以用下面的伪代码概括：

```
writeBytecode(os, module, targetVersion):
    1. verifySelfContainedModuleAndOperationInvariants(module)   # 前置校验
    2. config = { targetVersion }
    3. writeHeader(os, module, config)                            # magic + 版本
    4. new StringManager / TypeManager / ConstantManager / DebugInfoWriter
    5. new FunctionTableWriter(各 Manager, config)
    6. funcWriter.buildFunctionMap(module)                        # 预收集函数元数据
    7. writeGlobalSection(...)        # Global 段（可能跳过）
    8. funcWriter.writeFunctionTableSection(...)   # Func 段
    9. constantMgr.writeConstantSection(...)       # Constant 段（可能跳过）
   10. debuginfo.writeDebugInfoSection(...)        # Debug 段（可能跳过）
   11. typeMgr.writeTypeSection(...)               # Type 段
   12. writeProducerSection(...)                   # Producer 段（13.3+，可能跳过）
   13. stringMgr.writeStringSection(...)           # String 段
   14. os.write(EndOfBytecode)                      # 结束标记字节
```

两个要点先记在心里，后面会反复用到：

1. **顺序不是 section ID 的升序**。Global(0x06) 排在 Func(0x02) 前面，String(0x01) 反而排在几乎最后。这个「奇怪」的顺序是刻意设计的，是 4.4 节的核心。
2. **部分 section 会被整段跳过**（空表不写）。哪些会跳过、为什么，在 4.2 节给出。

#### 4.1.3 源码精读

入口函数本体：

[lib/Bytecode/Writer/BytecodeWriter.cpp:1673-1719](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1673-L1719) —— 这就是上面的伪代码对应的真实代码：先 `verifySelfContainedModuleAndOperationInvariants`，再 `writeHeader`，再依次实例化 Manager、`buildFunctionMap`、按序写出各 section，最后写一个 `EndOfBytecode` 字节。

注意它写出的 section 顺序与代码顺序完全一致：Global → Func → Constant → Debug → Type → Producer → String → End。

前置校验函数决定了「什么样的模块才能被序列化」：

[lib/Bytecode/Writer/BytecodeWriter.cpp:1629-1671](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1629-L1671) —— 它做两件事：(1) 模块顶层只允许 `FunctionOpInterface` 和 `GlobalOp`，其它操作一律拒绝（发出 remark 并失败）；(2) 模块内任意层级只允许 `cuda_tile` 方言的操作，并且 `reduce`/`scan` 体内只允许 `Pure` 操作。

这条校验的语义是「自包含（self-contained）」：字节码格式假设模块里没有外部依赖、没有跨方言的「外来」操作。如果你 lowering 进来的 IR 里混入了 `arith.addi` 这种 builtin 方言操作，就会在这里被拦下。

#### 4.1.4 代码实践

**实践目标**：亲手触发 `writeBytecode`，并观察「不自包含」时它如何拒绝写入。

1. 按README 示例把 print 内核存为 `example.mlir`（见 [README.md:233-246](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L233-L246)）。
2. 构建项目（参考 u1-l2），确保拿到 `cuda-tile-translate` 工具。
3. 正常序列化：
   ```bash
   cuda-tile-translate example.mlir --bytecode-version=13.1 \
       --mlir-to-cudatilebc --no-implicit-module -o example.tilebc
   ```
   预期：生成 `example.tilebc`，无报错。
4. 故意破坏自包含性：在内核体内插入一个 builtin 方言操作，例如加一行 `%x = arith.constant 0 : i32`，再次运行同一条命令。
5. **需要观察的现象**：第 4 步应出现类似 `only ops from the 'cuda_tile' dialect are allowed` 的 remark/error，且不产生 `.tilebc`——这正是 `verifySelfContainedModuleAndOperationInvariants` 在拦截。

> 第 3、4 步的具体输出**待本地验证**（取决于你的构建环境），但报错信息的关键短语应与上述源码字符串一致。

#### 4.1.5 小练习与答案

**练习 1**：`writeBytecode` 的第三个参数 `targetVersion` 不传时取什么值？为什么默认不是「最新版」13.3？

答案：不传时取 `BytecodeVersion::kCurrentCompatibilityVersion`，即 13.1（见头文件默认参数与 [Version.cpp:39-43](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L39-L43)）。默认给兼容版而非最新版，是为了让产出的 `.tilebc` 能被尽可能多的旧驱动加载——这是 u7-l1 讲过的「面向广泛兼容应显式/默认指向 13.1」在代码层的体现。

**练习 2**：如果模块顶层放一个既不是函数也不是 global 的操作，会怎样？

答案：`verifySelfContainedModuleAndOperationInvariants` 的第一个循环会对它 `emitRemark("invalid op: ")` 并返回失败，`writeBytecode` 立即 `return failure()`，不写出任何字节。

---

### 4.2 Section 枚举与整体二进制布局

#### 4.2.1 概念说明

整个 `.tilebc` 文件就是一个「头 + 一串段」的结构。源码文件顶部用注释画出了完整的文法：

[lib/Bytecode/Writer/BytecodeWriter.cpp:40-54](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L40-L54) —— 整体文法：`bytecode = header section*`；`header = magic[8] + 版本`；`section = sectionId[1] + length[varint] + (可选)alignment[varint] + (可选)padding + data`。

每一段（section）的开头都有一个**段头**，告诉读取器「我是哪种段、我的数据有多长、是否需要对齐」。段种类的编号定义在枚举里：

[lib/Bytecode/BytecodeEnums.h:30-42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h#L30-L42) —— 8 个 section ID：`EndOfBytecode=0x00, String=0x01, Func=0x02, Debug=0x03, Constant=0x04, Type=0x05, Global=0x06, Producer=0x07`。

> 注意：这个枚举给的是 section 的**身份编号**，不是它在文件里的**出现顺序**。读取器靠编号认人，不靠位置。

#### 4.2.2 核心流程

**头部（header）** 的真实写入逻辑：

[lib/Bytecode/Writer/BytecodeWriter.cpp:172-193](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L172-L193) —— 先校验目标版本不小于最低支持版（否则报错并给出合法区间）；然后写 8 字节 magic `{0x7F,'T','i','l','e','I','R',0x00}`；再用 `writeLE` 依次写 major(1 字节)、minor(1 字节)、tag(2 字节)。

这里有一个文档与实现的小出入需要提醒：文件顶部注释把版本写成 `version[varint]`（单数），但实现其实是 **三个定长小端字段**（major/minor 各 1 字节、tag 是 `uint16_t` 占 2 字节小端）。因此一个 13.1 的文件头部 12 字节是：

```
7F 54 69 6C 65 49 52 00   |   0D 01 00 00
└────── magic ──────────┘     └ major=13 minor=1 tag=0(2B) ┘
```

（`'T'=0x54 'i'=0x69 'l'=0x6C 'e'=0x65 'I'=0x49 'R'=0x52`。）

**段头（section header）** 的写入逻辑是理解整个格式的钥匙：

[lib/Bytecode/Writer/BytecodeWriter.cpp:199-211](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L199-L211) —— 把 section ID 的最高位（0x80）当「是否有对齐」的标志位：低 7 位存真正的 ID；如果 `alignment > 1`，就把最高位置 1，并在 `length` 之后额外写一个 `alignment` varint，再 `alignTo(alignment)` 填充 padding，使随后的数据落在对齐地址上。

也就是说，一个 section 在文件里长这样：

```
[ID|hasAlign]   length(varint)   [alignment(varint) 若 hasAlign]   [padding]   data[length 字节]
```

读取器读段头时：读 1 字节 → 低 7 位是 ID、最高位告诉它要不要再读一个 alignment varint → 读 length → 若有 alignment 则按它对齐 → 读 length 字节数据。`length` 只计数据本身，不含段头和 padding。

**段写出顺序**（来自 4.1 的入口函数）与 **空段跳过规则**：

| 顺序 | section（ID） | 写出函数 | 何时跳过 |
| --- | --- | --- | --- |
| 1 | Global (0x06) | `writeGlobalSection` | 没有 `global` 操作时整段不写 |
| 2 | Func (0x02) | `FunctionTableWriter::writeFunctionTableSection` | 总是写（哪怕 `numFunctions=0`） |
| 3 | Constant (0x04) | `ConstantManager::writeConstantSection` | 常量池为空时不写 |
| 4 | Debug (0x03) | `DebugInfoWriter::writeDebugInfoSection` | 无调试信息时不写 |
| 5 | Type (0x05) | `TypeManager::writeTypeSection` | 总是写 |
| 6 | Producer (0x07) | `writeProducerSection` | 版本 < 13.3 或模块无 producer 属性时不写 |
| 7 | String (0x01) | `StringManager::writeStringSection` | 总是写 |
| 末 | EndOfBytecode (0x00) | 直接写 1 字节 | 永不跳过 |

> 跳过逻辑要对照源码看：Global 在 [BytecodeWriter.cpp:1513-1514](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1513-L1514) 的 `if (globals.empty()) return success();`；Constant 在 [BytecodeWriter.cpp:528-529](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L528-L529)；Debug 在 [BytecodeWriter.cpp:636-637](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L636-L637)；Producer 在 [BytecodeWriter.cpp:1594-1599](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1594-L1599)。

#### 4.2.3 源码精读（对齐填充字节）

对齐填充用什么字节？由一个常量决定：

[lib/Bytecode/BytecodeEnums.h:24-27](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h#L24-L27) —— `kAlignmentByte = 0xCB`。`EncodingWriter::alignTo` 默认就用它当填充字节（见 4.3 节）。

> 提醒：文件顶部文法注释里把填充字节写成 `0xCF`（[BytecodeWriter.cpp:52](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L52)），而真正落盘的常量是 `0xCB`。以常量为准——这是源码里一处注释滞后于实现的小瑕疵，读源码时认 `kAlignmentByte`。

段内布局的注释（推荐对照阅读的「文法说明书」）散布在源码里，每个 Manager 上方都贴了对应段的文法，例如：

- String 段文法：[BytecodeWriter.cpp:238-243](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L238-L243)
- Type 段文法：[BytecodeWriter.cpp:301-326](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L301-L326)
- Constant 段文法：[BytecodeWriter.cpp:455-461](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L455-L461)
- Function 表文法：[BytecodeWriter.cpp:913-933](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L913-L933)
- Global 段、Producer 段文法：[BytecodeWriter.cpp:1499-1574](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1499-L1574)、[BytecodeWriter.cpp:1580-1586](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1580-L1586)

这些注释就是格式的「权威说明书」，读源码时优先看它们。

#### 4.2.4 代码实践

**实践目标**：在十六进制里亲眼看到 magic、版本字节和段头。

1. 用 4.1.4 的命令生成 `example.tilebc`。
2. 用 `xxd`（或 `hexdump -C`）查看头部 64 字节：
   ```bash
   xxd -l 64 example.tilebc
   ```
3. **需要观察的现象**（对照本节给出的字节布局）：
   - 前 8 字节应为 `7f 54 69 6c 65 49 52 00`（即 magic）；
   - 紧接 `0d 01 00 00`（13.1 版本：major=13、minor=1、tag=0）；
   - 之后第一个段头字节：低 7 位应是某个 section ID。因为示例没有 `global`，第一个段通常是 Func（`0x02`）。
4. 把你看到的每个段头字节的低 7 位与 `BytecodeEnums.h` 的枚举对照，在文件里标注「这一段是 Func、这一段是 String……」。

> 实际字节级输出**待本地验证**（取决于示例 IR 触发了哪些段、对齐情况如何），但 magic 与版本字节这两项是确定的。

#### 4.2.5 小练习与答案

**练习 1**：section ID 的「低 7 位 / 最高位」复用设计，好处是什么？

答案：用一个字节同时携带「段种类」和「是否带对齐」两件信息，省掉一个字节。读取器读 1 字节后，最高位若为 1 就额外读一个 alignment varint，否则跳过。代价是段种类最多只能有 128 种（低 7 位），目前用了 0x00–0x07 共 8 种，空间充裕。

**练习 2**：为什么 `EndOfBytecode` 是一个单独的字节，而不是某个段？

答案：它就是「段 ID = 0x00」、且不带 length/data 的特例——读取器读到 0x00 即知文件结束。把它放在枚举的 0 号位、且 `writeBytecode` 末尾单独 `os.write(EndOfBytecode)`（[BytecodeWriter.cpp:1717](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1717)），相当于一个不带载荷的终止哨兵。

---

### 4.3 EncodingWriter：变长整数、zigzag 与对齐

#### 4.3.1 概念说明

`EncodingWriter` 是写入器的「原子笔」——所有最终落到字节流上的编码都经过它。它封装了三种核心编码：

- **varint**（无符号变长整数）：小数字省字节；
- **signed varint / zigzag**（有符号变长整数）：让负数和小绝对值都能用很少的字节；
- **alignment**（对齐）：在需要的地方插入填充字节，让后续数据对齐。

此外它还提供 `writeLE`（定长小端整数、浮点）、`write(StringRef)`（裸字节）等便利方法。整个写入器里几乎所有 `write...` 调用都是它的成员。

#### 4.3.2 核心流程

**varint 编码**：每次取低 7 位，若还有剩余就把最高位（0x80）置 1 作为「续位」。解码时反向：读到最高位为 0 的字节就结束。

数学上，一个无符号值 \(v\) 的 varint 字节数约为 \(\lceil \mathrm{bits}(v)/7 \rceil\)。对 13.1 这样的「小数」只需 1 字节，对 64 位大数最多 10 字节。

**zigzag 编码**：直接对负数做 varint 会很费字节（补码下 -1 是全 1，要 10 字节）。zigzag 先做一次变换，把「绝对值小的整数」（无论正负）映射到「小的无符号数」：

\[
\mathrm{zz}(n) = (n \ll 1) \oplus (n \gg 63),\quad n \text{ 视为 64 位}
\]

效果是 \(0\to0,\ -1\to1,\ 1\to2,\ -2\to3,\ 2\to4\dots\)，正负交替铺成自然数，再走 varint 就省字节了。

**对齐**：`alignTo(a)` 计算当前到下一个 a 的倍数还差几个字节，逐个写填充字节补齐。

#### 4.3.3 源码精读

varint 实现：

[lib/Bytecode/Writer/BytecodeWriter.cpp:73-86](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L73-L86) —— 循环里每次 `byte = value & 0x7F` 取低 7 位，`value >>= 7` 右移，若还有剩余就 `byte |= 0x80` 置续位；用定长数组 `bytes[10]` 累积（64 位最多 10 字节），最后一次写出。注意它对枚举类型也有重载（[BytecodeWriter.cpp:88-91](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L88-L91)），把枚举转成底层整数再 varint。

zigzag 实现：

[lib/Bytecode/Writer/BytecodeWriter.cpp:97-99](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L97-L99) —— 一行表达式 `(value << 1) ^ (uint64_t)((int64_t)value >> 63)`。其中 `(int64_t)value >> 63` 是算术右移：负数得到全 1（即 `0xFFFF...FFFF`），非负得到全 0；与左移一位的值异或，恰好实现上面的 zz 映射。最终交给 `writeVarInt` 落盘。

定长小端与浮点：

[lib/Bytecode/Writer/BytecodeWriter.cpp:101-109](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L101-L109) —— `writeLE<T>` 对整型逐字节低位先写（小端）。浮点版本在 [BytecodeWriter.cpp:123-133](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L123-L133)，先 `static_assert` 要求 IEEE 754，再 `memcpy` 把浮点位模式拷到同等大小的整型（`uint32_t` 或 `uint64_t`），然后按整型小端写出——即「浮点按其 IEEE 位模式小端落盘」。

对齐：

[lib/Bytecode/Writer/BytecodeWriter.cpp:143-153](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L143-L153) —— `alignment < 2` 直接返回（1 字节对齐等于不对齐）；否则算 `padding = (alignment - currentPos % alignment) % alignment`，逐个写 `paddingByte`（默认 `Bytecode::kAlignmentByte` 即 0xCB）；并把传入的对齐要求与历史最大值取 `max`，记到 `requiredAlignment`，供段头决定是否需要写 alignment 字段。

`requiredAlignment` 与 4.2 节的段头最高位是配套的：每个 Manager 在自己的缓冲区上写数据时，`sectionWriter.getRequiredAlignment()` 会告诉你「这段数据最多用过多大的对齐」，`writeSectionHeader` 据此决定要不要置最高位、写 alignment varint。

#### 4.3.4 代码实践

**实践目标**：用纸笔或 REPL 验证 varint/zigzag 的编码结果，建立直觉。

1. 取版本号 `tag` 字段的写入为例：`writeLE<uint16_t>(0)` 会写出 `00 00` 两字节（定长，不是 varint）。
2. 手算 varint(300)：300 = `0b100101100`。低 7 位 `0101100`(0x2C) 置续位 → `0xAC`；剩余 `10`(0x02) 无续位 → `0x02`。所以 varint(300) = `AC 02`。
3. 手算 zigzag(-3)：按公式 \((-3 \ll 1) \oplus ((-3) \gg 63)\) = `(0xFFFFFFFFFFFFFFFA) ^ (0xFFFF...FFFF)` = `0x05`，再 varint → `05`。即 -3 编码成 1 字节 `05`。

**需要观察的现象**：理解为什么 `writeSignedVarInt` 对小负数也只要 1 字节。如果你会一点 C++，可以临时写个调用 `EncodingWriter::writeVarInt`/`writeSignedVarInt` 的小程序打印结果对照（**示例代码**，非项目原有代码）。

> 上述手算结果可对照 [BytecodeWriter.cpp:73-99](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L73-L99) 的实现自行核验；实际运行**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `writeVarInt` 的缓冲数组大小是 10？

答案：64 位无符号数最多需要 \(\lceil 64/7 \rceil = 10\) 字节（每个字节贡献 7 个有效位）。数组定为 `bytes[10]` 正好覆盖最坏情况，循环里同时用 `index < sizeof(bytes)` 做兜底防溢出。

**练习 2**：`alignTo` 里的 padding 公式 `(alignment - currentPos % alignment) % alignment`，外层那个 `% alignment` 作用是什么？

答案：当 `currentPos` 已经是 `alignment` 的整数倍时，`alignment - 0 = alignment`，若不再取一次模就会多写 `alignment` 个无用字节；外层 `% alignment` 把这种情况归零，做到「已经对齐就不补」。

---

### 4.4 各 Manager 类：表驱动的「收集在前、落盘在后」

#### 4.4.1 概念说明

`.tilebc` 里有一个非常关键的设计：**字符串、类型、常量都不在用到它们的地方就地写入，而是先收进一张「表」里，用索引（一个 varint）来引用；表本身作为独立 section 最后才落盘。**

这样做有三个好处：

1. **去重**：同一个字符串/类型/常量只存一份，多次引用复用同一索引，省空间；
2. **前向引用**：Func 段可以先写出来（里面只放索引），String/Type/Constant 表之后再补，因为索引在「收集」阶段就已确定；
3. **对齐友好**：表数据可以集中放在对齐位置，函数体里的索引只是小整数。

每个 Manager 就是「一张表 + 收集方法 + 落盘方法」。这正是 4.1 节里「String/Type 段写在最后」的根本原因：必须等所有引用方（Func/Global/Debug）都跑完，表才收集完整。

#### 4.4.2 核心流程

每个表 Manager 的内部套路高度一致，可以抽象成「offset 表 + 数据区」两段式：

```
section =:
    numEntries[varint]            # 表里有多少项
    padding                       # 对齐到 offset 表元素大小
    offsets[uint32/uint64]        # 每项数据在数据区的起始偏移
    data[bytes]                   # 真正的数据，逐项拼接
```

写入时有个统一的小技巧（在 String/Type/Constant/Debug 都用了）：**先在缓冲里占位写一串 0 当 offset 表，把当前指针记下来；然后边写数据边记录每项的真实偏移；最后回头把真实偏移填回那些占位的 0**。这就是源码里反复出现的 `offsetsPtr` + `std::copy_n(... finalOffsets ...)` 模式。

各 Manager 一览：

| Manager | 表的键 | 落盘 section | 对齐 | 备注 |
| --- | --- | --- | --- | --- |
| `StringManager` | `StringRef` | String (0x01) | 4 字节（`uint32_t` 偏移表） | 最基础的表，被几乎所有其它 Manager 依赖 |
| `TypeManager` | 类型的 opaque 指针 | Type (0x05) | 4 字节 | 先注册「依赖类型」再注册自己；序列化由生成的 `TypeBytecode.inc` 派发 |
| `ConstantManager` | `Attribute` | Constant (0x04) | 8 字节（`uint64_t` 偏移表） | 只存标量/dense 常量；空则跳过 |
| `DebugInfoWriter` | 调试属性的 opaque 指针 | Debug (0x03) | 多级（4/8 字节） | 自带「保留索引」机制给 UnknownLoc/FileLineColLoc |
| `FunctionTableWriter` | — | Func (0x02) | 8 字节 | 不去重；持有上面所有 Manager 的引用，是「总编排者」 |

#### 4.4.3 源码精读

**StringManager** —— 最简单、最典型的「表 Manager」：

[lib/Bytecode/Writer/BytecodeWriter.cpp:245-294](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L245-L294) —— `getStringIndex` 用 `MapVector` 去重并按首次插入顺序分配索引；`writeStringSection` 在缓冲上写 `numStrings`、对齐到 4、预留 `uint32_t` 偏移表（[BytecodeWriter.cpp:268-269](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L268-L269) 占位写 0），逐串写出数据并累计偏移，最后 `std::copy_n` 把偏移回填（[BytecodeWriter.cpp:283-284](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L283-L284)）。`MapVector` 保证「同样的字符串拿到同样索引」且顺序稳定。

**TypeManager** —— 多了「依赖类型先注册」与「生成代码派发」：

[lib/Bytecode/Writer/BytecodeWriter.cpp:328-449](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L328-L449) —— `getTypeIndex`（[BytecodeWriter.cpp:333-345](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L333-L345)）在登记一个类型前先调 `registerDependentTypes`，确保嵌套类型（如 `tile<...>` 的元素类型、函数类型的输入输出）先入表，这样序列化时引用的是「已存在的更小索引」。真正的序列化由 `#include "TypeBytecode.inc"` 注入的 `GEN_TYPE_WRITERS`/`GEN_TYPE_WRITER_DISPATCH`（[BytecodeWriter.cpp:397-404](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L397-L404)）按 `TypeTag` 派发；函数类型是手写的特例 `serializeFunctionType`（[BytecodeWriter.cpp:406-425](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L406-L425)），写 tag、输入数、各输入类型索引、结果数、各结果类型索引。

**ConstantManager** —— 偏移表元素是 `uint64_t`（故 8 字节对齐）：

[lib/Bytecode/Writer/BytecodeWriter.cpp:464-574](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L464-L574) —— `addConstant` 把属性序列化到一个 `SmallVector<char>` 缓冲里并按属性去重；`serializeAttribute`（[BytecodeWriter.cpp:501-524](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L501-L524)）只支持 `DenseElementsAttr`（写「长度 + 裸数据」）、`IntegerAttr`、`BoolAttr`、`FloatAttr` 四类，其它报错。空池直接跳过（[BytecodeWriter.cpp:528-529](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L528-L529)）。

**DebugInfoWriter** —— 结构最复杂，有「保留索引」机制：

[lib/Bytecode/Writer/BytecodeWriter.cpp:581-906](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L581-L906) —— `getDebugReserved`（[BytecodeWriter.cpp:764-769](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L764-L769)）把 `UnknownLoc`/`FileLineColLoc` 映射到保留索引 0（`DebugReserved::UnknownLoc`），不占表项；其余调试属性才进 `debuginfoList`，且索引从 `SIZE`(=1) 起算（[BytecodeWriter.cpp:597-601](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L597-L601)、[BytecodeWriter.cpp:743-748](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L743-L748)）。段内分三块：每个 op 的调试索引偏移表（`uint32_t`）、调试索引数组（`uint64_t`）、调试属性数据区（带 `uint32_t` 偏移表），见文法注释 [BytecodeWriter.cpp:618-633](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L618-L633)。各 `DI*` 属性的序列化是手写的 `serialize(...)` 重载（[BytecodeWriter.cpp:804-899](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L804-L899)），内容承接 u6-l3 的 DI 属性层级。

**FunctionTableWriter** —— 总编排者，串起所有表：

[lib/Bytecode/Writer/BytecodeWriter.cpp:948-1497](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L948-L1497) —— 它持有 `typeMgr/constMgr/strMgr/debuginfo` 的引用。`buildFunctionMap`（[BytecodeWriter.cpp:1338-1366](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1338-L1366)）先收集每个函数的名字索引、签名索引、位置索引、是否 entry、优化提示。`writeFunctionTableSection`（[BytecodeWriter.cpp:1368-1420](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1368-L1420)）逐函数写元数据 + 一个 `entryFlag` 字节（位含义见 [BytecodeEnums.h:66-73](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h#L66-L73)：bit0 私有可见性、bit1 kernel、bit2 带优化提示）+ 函数体。

函数体由 `writeFunctionBody`（[BytecodeWriter.cpp:1319-1335](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1319-L1335)）逐指令写出，核心是 `writeOperation`（[BytecodeWriter.cpp:954-986](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L954-L986)）：查 opcode 表、做版本可用性校验（`isOpcodeAvailableInVersion`）、写 opcode varint、再由 `#include "Bytecode.inc"` 注入的 `dispatchOpWriter` 写操作数/结果/属性。操作数与结果都通过 `valueIndexMap` 转成「函数内 SSA 值编号」的 varint（见 `writeOperands` [BytecodeWriter.cpp:996-1004](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L996-L1004)）。属性写入由 `writeSingleAttribute`（[BytecodeWriter.cpp:1042-1201](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1042-L1201)）按属性类型分流：基础属性（Type/String/Integer/Float/Bool/DenseElements/Array/Dictionary）就地写或写索引；cuda_tile 专有属性（`DivBy`/`SameElements`/`OptimizationHints`/`Bounded`）走 `WRITE_VERSIONED_ATTR_TAG` 宏（[BytecodeWriter.cpp:1026-1037](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1026-L1037)）做版本校验后写 tag + 数据。

**Global 段与 Producer 段** —— 体现版本相关的写法：

[lib/Bytecode/Writer/BytecodeWriter.cpp:1500-1575](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1500-L1575) —— Global 段每个 global 写「名字索引 + 类型索引 + 常量索引 + 对齐」共 4 个 varint；仅当目标版本 ≥ 13.3 才额外写「可见性 + constant 标志」两个字段（[BytecodeWriter.cpp:1562-1566](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1562-L1566)）。若目标版本 < 13.3 却遇到非 public 可见性或 `constant` global，会**拒绝写出**（[BytecodeWriter.cpp:1524-1540](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1524-L1540)），以免静默丢失语义——这是 u7-l4 版本兼容策略在写入侧的硬约束。

[lib/Bytecode/Writer/BytecodeWriter.cpp:1587-1614](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1587-L1614) —— Producer 段只在 ≥ 13.3 且模块带 `producer` 属性时写一个字符串索引。

#### 4.4.4 代码实践

**实践目标**：通过「对比两次序列化」验证去重与前向引用。

1. 用 4.1.4 的命令分别生成两个文件：
   - `a.tilebc`：示例 IR 原样；
   - `b.tilebc`：在 IR 里再重复用一次同一个字符串字面量（例如多加一行 `print_tko "Data: %f\n", ...`），观察 String 段大小是否**几乎不变**（去重生效）。
2. 对 `a.tilebc` 跑 `xxd | tail`，定位最后一段（应是 String 段，ID 低 7 位 = 0x01），其后紧跟 `00`（EndOfBytecode）。这验证了「String 段写在最后」。
3. 再往前找 Func 段（ID 低 7 位 = 0x02），它在 String 段**之前**出现，但里面引用的字符串索引指向「后面才出现」的 String 表——这就是前向引用。

**需要观察的现象**：`b.tilebc` 的 String 段不因重复字符串而增长；Func 段出现在 String 段之前。

> 字节级精确长度**待本地验证**，但「重复字符串不增表长」「Func 先于 String 出现」这两条结构性结论由源码逻辑保证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TypeManager::getTypeIndex` 要先 `registerDependentTypes` 再登记自己？

答案：序列化一个复合类型（如 `tile<4x8xf32>`）时，要先写出它的元素类型索引，再写自身形状。元素类型索引必须指向「已经在表里」的类型。先登记依赖类型，就保证了「被引用者的索引 < 引用者的索引」，序列化时数据自洽，读取器也能按顺序重建。

**练习 2**：`FunctionTableWriter` 不是去重表，它和四个 Manager 的关系是什么？

答案：它是「总编排者」，持有四个 Manager 的引用。函数体里的操作数、类型、属性、字符串、调试信息都通过对应 Manager 转成索引再写出。换句话说，Func 段本身存的是「指令流 + 一堆索引」，真正的字符串/类型/常量数据由各 Manager 各自落盘到独立 section。这也是 Func 段能先写、各表段后写的原因。

**练习 3**：Global 段在 13.1 目标下遇到 `constant` global 为什么直接报错，而不是「降级」写出去？

答案：字节码格式的字段布局是「按版本固定长度/个数的」——13.1 的 Global 每条固定 4 个 varint，13.3 才多出可见性与 constant 两个字段。如果偷偷把 constant global 按 13.1 写出，读取器会把后续数据错位解析，且静默丢失「这是常量」的语义。直接拒绝写出是最安全的选择（见 [BytecodeWriter.cpp:1524-1540](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L1524-L1540)）。

---

## 5. 综合实践

把本讲三块知识（入口编排、section 布局、编码与 Manager）串起来，完成一次「手动反汇编 `.tilebc` 头部」的任务。

1. **准备 IR**：使用 [README.md:233-246](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L233-L246) 的 `example.mlir`。
2. **生成字节码**：
   ```bash
   cuda-tile-translate example.mlir --bytecode-version=13.1 \
       --mlir-to-cudatilebc --no-implicit-module -o example.tilebc
   ```
3. **十六进制转储**：`xxd example.tilebc > example.hex`。
4. **在 `example.hex` 上做标注**（写在注释或旁边）：
   - 第 0–7 字节：magic `7f 54 69 6c 65 49 52 00`；
   - 第 8–11 字节：版本 `0d 01 00 00`（13.1.0）；
   - 从第 12 字节起，逐段读：每段先读 1 字节段头 → 低 7 位查 `BytecodeEnums.h` 的 `Section` 枚举得到段种类、最高位判断是否带对齐 → 读 length varint →（若带对齐）读 alignment varint 并跳过 padding → 跳过 length 字节数据 → 进入下一段；
   - 依次标注出你识别到的每个段（Func / Constant / Type / String 等），直到读到 `00`（EndOfBytecode）。
5. **对照源码核验**：把你标注的段顺序与 4.2.2 节的「段写出顺序表」对比；把你看到的填充字节与本节说的 `kAlignmentByte = 0xCB` 对比。
6. **再生成一个 13.3 版本**：把 `--bytecode-version=13.1` 换成 `--bytecode-version=13.3`，重复转储与标注，对比两者差异（13.3 多出 Producer 段、Global 段字段更多等）。

**预期结果**：你能用源码解释 `.tilebc` 前 N 个字节每一个的含义，并且 13.1 与 13.3 两份文件的结构差异与你从源码读到的版本分支一致。

> 字节级精确内容**待本地验证**（取决于实际 IR 与对齐），但「能逐段解释、13.1/13.3 差异符合源码分支」是判定成功的标准。

## 6. 本讲小结

- `.tilebc` = `header + section*`；header 是 8 字节 magic `{0x7F,'T','i','l','e','I','R',0x00}` + 三个定长小端版本字段（major/minor/tag），不是单个 varint。
- 每个 section 的段头 = `[ID|hasAlign]` 1 字节 + length varint +（可选）alignment varint +（可选）padding + data；ID 低 7 位是种类，最高位表示是否带对齐。
- 段写出顺序是 Global → Func → Constant → Debug → Type → Producer → String → End，**不是** ID 升序；空段（无 global、无常量、无调试、< 13.3 的 Producer）整段跳过。
- `EncodingWriter` 提供三种核心编码：varint（无符号变长）、zigzag + varint（有符号变长）、`alignTo`（对齐填充，填充字节为 `kAlignmentByte = 0xCB`）。
- 四个表 Manager（String/Type/Constant/Debug）都用「offset 表 + 数据区」两段式，并通过 `offsetsPtr` + `std::copy_n` 回填偏移；`FunctionTableWriter` 是总编排者，把指令流里的字符串/类型/常量/调试信息全部转成索引。
- 整套设计的关键是「**收集在前、落盘在后**」：Func 段先写但只含索引，被引用的 String/Type/Constant 表最后才落盘，靠索引实现前向引用与去重。

## 7. 下一步学习建议

- **下一篇 u7-l3（字节码读取器）** 会讲反序列化：`BytecodeReader` 如何读 magic/header、按 section 重建各张表、惰性构建类型表、逐指令 `parseOperation`。读完它你就能双向理解整个格式。
- **u7-l4（字节码版本与兼容性）** 会展开本讲多次出现的 `sinceVersion`、`isOpcodeAvailableInVersion`、13.1↔13.3 字段差异背后的版本模型。
- 若想理解本讲里 `#include "Bytecode.inc"` / `"TypeBytecode.inc"` 这些生成代码从何而来，去看 **u8-l1（cuda-tile-tblgen 字节码代码生成）**。
- 想亲眼对照「文法说明书」，推荐重读 [BytecodeWriter.cpp:34-54](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L34-L54) 与各 Manager 上方的段文法注释。
