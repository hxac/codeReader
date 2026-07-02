# 字节码读取器与反序列化

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `readBytecode` 作为反序列化总入口的「两阶段」设计：先线性扫描把所有 section 的 payload 收集起来，再按**依赖顺序**逐一解析；
- 在源码中定位 header 与 section 帧的解析逻辑（`parseHeader` / `parseSectionHeader`），讲清楚 magic 校验、版本校验、section ID/length/alignment 是怎么读出来的；
- 解释 `EncodingReader` 的三种核心解码：varint、有符号 varint（zigzag）、对齐跳过（`skipPadding`），以及它如何承载「带偏移的错误信息」；
- 说出 `LazyTypeTable`、`DenseElementsAttrCache`、`DebugInfoReader` 三张表「惰性解析 + 缓存 + 递归保护」的共性设计；
- 讲清楚 `createFunction` / `createGlobal` 如何把表段里收集到的元数据重建为真实的 `EntryOp` / `GlobalOp`，以及 `InstructionParser::parseOperation` 如何逐条指令恢复函数体；
- 学会用 `test/Bytecode/invalid/` 下的非法字节码文件触发读取器，并对照源码核对每种错误信息的来源。

本讲只讲**读取端**（反序列化），是 u7-l2（写入端）的对偶。两讲共用同一份二进制格式规约，因此本讲会频繁回指 u7-l2 中的 magic、section 布局与各表段。

## 2. 前置知识

本讲假设你已经掌握 u7-l1 与 u7-l2，特别是：

- `.tilebc` 的整体结构是 `header + section*`，header 是 8 字节 magic `{0x7F,'T','i','l','e','I','R',0x00}` 加上 major/minor/tag 三个定长小端版本字段；
- 每个 section 的帧格式是 `[sectionId 字节 | length varint | 可选 alignment varint | 可选 padding | data]`，sectionId 的高位（0x80）是「是否带对齐」的标志；
- 写入端按 `Global→Func→Constant→Debug→Type→Producer→String→End` 的顺序落盘，并用 `StringManager / TypeManager / ConstantManager / DebugInfoWriter / FunctionTableWriter` 五张「offset 表 + 数据区」两段式结构，靠**索引**互相引用；
- 字节码版本有「最低版 13.1 / 当前兼容版 13.1 / 当前版 13.3」之分，合法区间是 [13.1, 13.3]；
- MLIR 的基本对象：`ModuleOp`、`Operation`、`Region`/`Block`、`Type`、`Attribute`、`Value`（SSA 值）。

另外补充两个本讲会用到的 MLIR/编译概念：

- **反序列化（deserialization）**：把字节流重新拼装成内存里的 IR 对象树。读取器本质上是一个「按格式契约逐字节消费」的状态机，每读一段都要做越界与一致性校验，防止畸形输入导致崩溃或读出无意义对象。
- **惰性解析（lazy parsing）**：类型表、调试信息表可能很大，但一次反序列化未必用到全部条目。读取器只把整张表的原始字节记下来，**谁被引用才解析谁**，并缓存结果，避免重复解析与为无用条目付代价。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lib/Bytecode/Reader/BytecodeReader.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp) | 读取器全部实现：`EncodingReader` 解码工具、`parseHeader`/`parseSectionHeader`、各 section 解析函数、`LazyTypeTable`/`DenseElementsAttrCache`/`DebugInfoReader`、`InstructionParser`、`createFunction`/`createGlobal`、`readBytecode`/`getBytecodeSize`/`isTileIRBytecode`。 |
| [include/cuda_tile/Bytecode/Reader/BytecodeReader.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Reader/BytecodeReader.h) | 对外只暴露三个函数：`isTileIRBytecode`、`getBytecodeSize`、`readBytecode`。 |
| [lib/Bytecode/BytecodeEnums.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h) | `Section` 枚举（各段 ID）、`DebugTag`、`FunctionFlags`、对齐填充字节常量 `kAlignmentByte`。 |
| [include/cuda_tile/Bytecode/Common/Version.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/Version.h) / [lib/Bytecode/Common/Version.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp) | `BytecodeVersion` 类型、各版本常量取值，以及 opcode 的版本可用性检查 `isOpcodeAvailableInVersion`。 |
| [test/Bytecode/invalid/invalid_structure.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/invalid/invalid_structure.mlir) | 一组「畸形字节码」的 FileCheck 测试，是本讲实践的依据。 |

> 说明：读取器 `#include` 了若干由 `cuda-tile-tblgen` 生成的胶水文件：`StaticOpcodes.inc`（opcode 枚举）、`BytecodeReader.inc`（`GEN_OP_READER_DISPATCH`，按 opcode 分派到各 `parse<Op>` 的 switch）、`TypeBytecodeReader.inc`（`GEN_TYPE_READERS` / `GEN_TYPE_READER_DISPATCH`，按 typeTag 分派）、`AttrBytecode.inc`（枚举属性的版本检查与符号化）。这些生成代码的来源见 u8-l1。本讲把它们当黑盒，只关注「生成代码在解析流程里扮演什么角色」。

## 4. 核心概念与源码讲解

### 4.1 readBytecode 入口：两阶段解析与编排

#### 4.1.1 概念说明

`readBytecode` 是反序列化的总入口，在头文件里只有一行声明，是读取端对外暴露的**唯一**「真正解析」函数：

[include/cuda_tile/Bytecode/Reader/BytecodeReader.h:29-30](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Reader/BytecodeReader.h#L29-L30) —— 接收一段字节缓冲与一个 `MLIRContext`，返回一个 `OwningOpRef<cuda_tile::ModuleOp>`（拥有所有权的模块）。

它的核心职责和写入端 `writeBytecode` 一样，不是「亲手读每一个字节」，而是**当指挥**：先解析 header 拿到版本，再线性扫描把每个 section 的原始 payload 存进一个按 section ID 索引的数组，最后**按依赖顺序**把 payload 喂给各 `parseXxxSection`，并在末尾创建 `ModuleOp`、逐个 `createGlobal` / `createFunction`，最后跑一次 MLIR `verify`。

这种「先发现、后解析」的两阶段设计带来一个直接好处：**读取器不依赖文件里 section 的物理顺序**。写入端按某个固定顺序落盘只是惯例，读取端真正关心的是依赖关系——必须先有字符串表，类型表里的函数类型才能解析（函数类型存的是类型索引）；必须先有类型表与常量表，函数/全局的重建才能进行。所以读取端把顺序控制权握在自己手里。

#### 4.1.2 核心流程

`readBytecode` 的执行顺序可以概括为：

```
readBytecode(buffer, context):
    reader = EncodingReader(buffer)                      # 构造解码器
    debuginfo = DebugInfoReader(context, reader)         # 调试信息读取器（引用主 reader 的字符串表）
    1. parseHeader(reader, version)                      # 校验 magic + 版本，得到 bytecodeVersion
    2. while true:                                       # 第一阶段：发现所有 section
         parseSectionHeader(reader, header)              #   读 sectionId/length/alignment
         校验 section 对齐是否满足
         payload = reader.readBytes(header.length)       #   把整段 payload 切出来
         sectionPayloads[header.sectionID] = payload
         if header.sectionID == EndOfBytecode: break
    3. 按依赖顺序解析（用存好的 payload）:
         String(必填) → Producer(可选) → Type → Constant → Global → Func(必填) → Debug
    4. 创建 ModuleOp（名字 "kernels"），再 createGlobal*、createFunction*
    5. verify(module)                                     # MLIR 全量校验，失败则返回 nullptr
    return module
```

注意第 3 步里 **String 和 Func 两段是必填的**：缺字符串表就无法解释任何名字，缺函数段则模块为空——这两段缺失会直接报错返回。其余段（Producer/Type/Constant/Global/Debug）都是可选的，存在才解析。

#### 4.1.3 源码精读

`readBytecode` 的入口与字节缓冲封装：

[lib/Bytecode/Reader/BytecodeReader.cpp:2461-2473](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2461-L2473) —— 把 `MemoryBufferRef` 转成 `ArrayRef<uint8_t>`，构造 `EncodingReader` 与 `DebugInfoReader`，并调用 `parseHeader` 拿到版本；header 解析失败则带错误信息返回 `nullptr`。

第一阶段的「发现循环」与 section payload 收集：

[lib/Bytecode/Reader/BytecodeReader.cpp:2479-2540](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2479-L2540) —— 循环 `parseSectionHeader`，遇到 `EndOfBytecode` 时还要确认它之后**没有剩余字节**（否则报「end section is not the last section」）；每读一段都按 section ID 做对齐校验（`validateSectionAlignment`），再用 `readBytes(length)` 切出 payload 存进 `sectionPayloads[header.sectionID]`。

第二阶段的「按依赖顺序解析」与最终的模块创建：

[lib/Bytecode/Reader/BytecodeReader.cpp:2551-2620](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2551-L2620) —— 依次解析 String（必填）→Producer→Type→Constant→Global→Func（必填）→Debug；随后创建 `ModuleOp`，先建全部 `GlobalOp` 再建全部 `EntryOp`，最后 `verify`。

附带两个「轻量」入口。`isTileIRBytecode` 只看头 8 字节是不是 magic，用于快速判定一段缓冲是否值得当作字节码处理：

[lib/Bytecode/Reader/BytecodeReader.cpp:42-58](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L42-L58) —— `MemoryBufferRef` 重载用 `memcmp` 比较 magic；`const char*` 重载因 magic 以 `\0` 结尾，用 `strnlen` 取长度再委托给前者。

`getBytecodeSize` 则更巧妙——它**不重建任何 IR**，只是顺着 header 与 section 帧往前走，直到 `EndOfBytecode`，用「已读偏移」当作真实大小返回。为此它新建一个临时 `MLIRContext`、用 `ScopedDiagnosticHandler` 把所有诊断吞掉，并把缓冲当作「极大长度」来解析：

[lib/Bytecode/Reader/BytecodeReader.cpp:2416-2459](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2416-L2459) —— 通过解析来「推断」字节码真实大小；任何一帧解析失败、section 重复、或 EndOfBytecode 没出现，都返回 `nullopt`。

#### 4.1.4 代码实践

**实践目标**：验证「读取器不依赖 section 的物理顺序」这一结论。

**操作步骤**：

1. 构建项目（见 u1-l2），确保 `cuda-tile-translate` 与 `cuda-tile-opt` 可用。
2. 准备一个最小合法 MLIR（参考 `test/Bytecode/operationsTest.mlir` 的写法），例如只含一个 `entry` 与一条 `return`：
   ```mlir
   cuda_tile.module @kernels {
     cuda_tile.entry @return_op(%a: !cuda_tile.tile<i32>) {
       cuda_tile.return
     }
   }
   ```
3. 用 round-trip 脚本验证它能正常往返：`%round_trip_test %s %t`（脚本逻辑见 [test/round_trip_test.py](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/round_trip_test.py)，它会 `mlir-to-cudatilebc` 再 `cudatilebc-to-mlir`，并与 `cuda-tile-opt` 的输出做 diff）。
4.（源码阅读型）在源码中确认：`readBytecode` 的发现循环只往 `sectionPayloads[]` 里按 ID 存 payload，**没有任何对 section 出现先后顺序的假设**；真正决定解析顺序的是第 2551–2595 行那段 `if (sectionPayloads[...].has_value())` 的固定序列。

**需要观察的现象**：round-trip 通过，说明读取器正确重建了模块。

**预期结果**：终端打印 `Round-trip test passed`。

> 若本地尚未构建，可标注「待本地验证」，先完成第 4 步的纯源码阅读部分。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `readBytecode` 要把所有 section 先存起来，而不是「读到 String 段就立刻解析、读到 Type 段就立刻解析」？

**参考答案**：因为段与段之间存在依赖，且文件里的物理顺序未必匹配依赖顺序。例如函数段引用类型索引，而类型段可能在文件里排在函数段之后（写入端把 Func 排在 Type 之前）。两阶段设计把「按文件顺序读字节」和「按依赖顺序建对象」解耦，让读取器对物理顺序鲁棒。

**练习 2**：`getBytecodeSize` 为什么不直接返回 `buffer.getBufferSize()`，而要走一遍解析？

**参考答案**：调用方传入的缓冲可能比真实字节码大（例如字节码嵌在更大的数据块里），`getBytecodeSize` 的职责是给出「字节码本身」的长度，所以必须顺着格式往前走、在 `EndOfBytecode` 处停下，用已读偏移作为真实大小。

---

### 4.2 header 与 section 帧解析：magic、版本与 section 校验

#### 4.2.1 概念说明

本模块讲读取器最底层的两件事：怎么读 header（magic + 版本），怎么读每一个 section 的帧头（ID + length + alignment）。这两件事共用同一个解码工具 `EncodingReader`，它是整份读取器的基础设施——所有「按字节读」最终都落到它身上。

`EncodingReader` 维护一段 `ArrayRef<uint8_t>` 与一个当前偏移 `offset`，提供三类核心能力：

- **varint 解码**：写入端用「低 7 位存数据、最高位当续位标志」编码无符号整数，读取端用 `readVarInt` 还原；
- **有符号 varint（zigzag）解码**：`readSignedVarInt` 先读一个 varint，再做 zigzag 反变换 `(x >> 1) ^ -(x & 1)`，把有符号数映射回原值；
- **对齐跳过**：`skipPadding(alignment)` 按当前偏移计算需要补几个填充字节并跳过，让后续定长数据落在对齐地址上（这样定长数组能直接 `reinterpret_cast` 读出，见 4.3）。

此外，`EncodingReader::emitError()` 会自动带上「出错偏移」，让每条错误信息都形如 `error at offset N: ...`，这对调试畸形字节码极其有用。

#### 4.2.2 核心流程

**header 解析**（`parseHeader`）：

```
parseHeader(reader, version):
    for i in 0..7:
        byte = reader.readLE<uint8_t>()
        if byte != kTileIRBytecodeMagic[i]:
            return error("invalid magic number at position i, got byte expected magic[i]")
    verMajor = reader.readLE<uint8_t>()
    verMinor = reader.readLE<uint8_t>()
    tag      = reader.readLE<uint16_t>()            # 小端
    v = BytecodeVersion::fromVersion(verMajor, verMinor, tag)
    if v 不存在 or v < kMinSupportedVersion(13.1):
        return error("unsupported Tile version M.m.tag, this reader supports [13.1 - 13.3]")
    version = v
```

**section 帧头解析**（`parseSectionHeader`）：

```
parseSectionHeader(reader, header):
    idAndAligned = reader.readLE<uint8_t>()
    header.sectionID    = idAndAligned & 0x7F       # 低 7 位是 ID
    header.hasAlignment = (idAndAligned & 0x80) != 0 # 最高位是对齐标志
    if sectionID == EndOfBytecode:
        若 hasAlignment 为真则报错（结束段不该带对齐）；直接返回（不读 length）
    if sectionID >= NumSections: return error("unknown section ID")
    header.length = reader.readVarInt()
    if length > remaining: return error("section length 超过剩余数据")
    if hasAlignment:
        header.alignment = reader.readVarInt()
        校验 alignment 非 0 且为 2 的幂
        reader.skipPadding(alignment)               # 跳过填充字节
```

注意一个关键细节：**`EndOfBytecode`（值为 0）的段头不携带 length**，它只是一个「到此为止」的标记。`parseSectionHeader` 在识别到它时直接返回，不去读后续的 length 字段——这也是 4.1.4 实践里 `excessive_section_length.tileirbc` 能触发「end section is not the last section」的原因。

#### 4.2.3 源码精读

magic 常量定义：

[lib/Bytecode/Reader/BytecodeReader.cpp:38-40](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L38-L40) —— 8 字节 magic，首字节是 `0x7F`，末字节是 `0x00`（null 结尾，供 `isTileIRBytecode(const char*)` 用 `strnlen` 取长度）。

`EncodingReader` 的三种核心解码：

[lib/Bytecode/Reader/BytecodeReader.cpp:94-113](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L94-L113) —— `readVarInt`：循环取字节、取低 7 位、按 7 位左移累加，直到高位续位标志为 0；并支持一个可选上限 `max`，越界即报错。

[lib/Bytecode/Reader/BytecodeReader.cpp:118-124](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L118-L124) —— `readSignedVarInt`：先 `readVarInt`，再做 zigzag 反变换。

[lib/Bytecode/Reader/BytecodeReader.cpp:251-269](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L251-L269) —— `skipPadding` 按 `(alignment - offset%alignment) % alignment` 算填充量并跳过；`emitError` 自动附带 `error at offset N:`。

`parseHeader` 的逐字节 magic 校验与版本校验：

[lib/Bytecode/Reader/BytecodeReader.cpp:299-328](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L299-L328) —— 逐字节比对 magic，任一字节不符即报「invalid magic number at position i」；版本字段用定长 `readLE` 读出，经 `BytecodeVersion::fromVersion` 构造，低于 `kMinSupportedVersion` 即报「unsupported Tile version」并列出支持区间。

`parseSectionHeader` 的 ID/length/alignment 处理：

[lib/Bytecode/Reader/BytecodeReader.cpp:331-372](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L331-L372) —— 拆出低 7 位 ID 与高位对齐标志；`EndOfBytecode` 早返回；ID 越界（≥ `NumSections`）报「unknown section ID」；length 超过剩余数据报错；带对齐时校验 alignment 是 2 的幂并跳过 padding。

`Section` 枚举（各段 ID 的单一数据源）：

[lib/Bytecode/BytecodeEnums.h:30-42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h#L30-L42) —— `EndOfBytecode=0`、`String=1`、`Func=2`、`Debug=3`、`Constant=4`、`Type=5`、`Global=6`、`Producer=7`、`NumSections=8`。`NumSections` 既当上界用于「unknown section ID」判定，也当 `sectionPayloads` 数组的容量。

各版本常量取值：

[lib/Bytecode/Common/Version.cpp:39-64](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L39-L64) —— 兼容版/最低版都是 13.1，当前版 13.3；统一 bitfield 版本（13.3）会影响可选参数的解码。

#### 4.2.4 代码实践

**实践目标**：用 `test/Bytecode/invalid/` 下的四个非法字节码文件触发读取器，对照源码核对每种错误信息来自哪一行。

**操作步骤**：

1. 这些非法文件由 [test/Bytecode/invalid/invalid_structure.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/invalid/invalid_structure.mlir) 驱动，它对每个文件写了一条形如 `// RUN: not cuda-tile-translate -cudatilebc-to-mlir %S/<file> -no-implicit-module 2>&1 | FileCheck %s --check-prefix=XXX` 的用例，`not` 表示「命令必须以非零码失败」。
2. 单独运行其中一条（以 magic 为例），手动观察报错：
   ```
   cuda-tile-translate -cudatilebc-to-mlir test/Bytecode/invalid/invalid_magic_number.tileirbc -no-implicit-module
   ```
3. 对照下表，把每个文件的字节、触发位置、源码行、预期错误信息一一对应。

| 文件 | 关键字节 | 触发位置 | 源码 | 预期错误（FileCheck 前缀） |
| --- | --- | --- | --- | --- |
| `invalid_magic_number.tileirbc` | 首字节 `0x7E`(126) 而非 `0x7F`(127) | `parseHeader` 第 0 字节比对 | BytecodeReader.cpp:302-309 | `MAGIC: invalid magic number` |
| `unsupported_version.tileirbc` | 版本字段 `18.0.0` | `parseHeader` 版本校验 | BytecodeReader.cpp:317-325 | `VERSION: unsupported Tile version 18.0.0, this reader supports versions [13.1 - 13.3]` |
| `invalid_section_id.tileirbc` | sectionId 字节 `0x7F`→ID=127，≥`NumSections`(8) | `parseSectionHeader` ID 越界 | BytecodeReader.cpp:350-351 | `SECTION_ID: unknown section ID: 127` |
| `excessive_section_length.tileirbc` | sectionId 字节 `0x00`=EndOfBytecode，但其后仍有字节 | `readBytecode` EndOfBytecode 后剩余非 0 | BytecodeReader.cpp:2486-2490 | `SECTION_LENGTH: end section is not the last section` |

**需要观察的现象**：每个文件都让 `cuda-tile-translate` 以非零码退出，stderr 打印与上表「预期错误」一致。

**预期结果**：四条命令全部失败且错误信息命中对应 FileCheck 前缀；若想一键跑全部，执行 `cmake --build build --target check-cuda-tile` 并在输出里找到 `invalid_structure.mlir` 通过。

> 若本地尚未构建，可标注「待本地验证」，先完成「字节→源码行→错误信息」的对照表填写。

#### 4.2.5 小练习与答案

**练习 1**：`parseSectionHeader` 对 `EndOfBytecode` 段头为什么「直接返回、不读 length」？如果它也读 length 会怎样？

**参考答案**：因为 `EndOfBytecode` 只是结束标记，规约上不携带 length。若强行读 length，会把填充/后续字节误当 length，并可能把真正的结束判断搞乱；同时 `readBytecode` 依赖「EndOfBytecode 之后必须无剩余字节」这一不变量来确认文件完整，乱读 length 会破坏该校验。

**练习 2**：构造一个 sectionId 字节为 `0x85`（即 `1000 0101`）的段头，读取器会如何解释？

**参考答案**：低 7 位 `000 0101` = 5 = `Section::Type`，最高位 `1` 表示「带对齐」。所以读取器会把它当作一个带对齐的 Type 段，继续读 length varint、alignment varint 并跳过 padding。这正是「高位是对齐标志」机制的体现。

---

### 4.3 表段与惰性缓存设计：String / Type / Constant / Debug

#### 4.3.1 概念说明

字节码里有四张「表段」：String（字符串表）、Type（类型表）、Constant（常量表）、Debug（调试信息表）。它们有高度一致的结构：**一个偏移数组 + 一段连续数据区**，条目之间靠**索引**互相引用（详见 u7-l2 的写入端设计）。

读取端对这四张表有一个共同的优化思想——**惰性解析 + 缓存**：

- 解析段时**只读出偏移数组与数据区指针**，不为每个条目真正构造 MLIR 对象；
- 真正构造发生在「某个条目被索引引用」时（按需触发）；
- 构造结果**缓存**进一个 `vector`，下次再被引用直接返回；
- 由于类型/调试信息可能递归引用（如函数类型引用其它类型、词法块引用上层作用域），还需要**递归保护**，防止无限递归。

String 段是个例外：字符串没有「懒」的必要（解析开销几乎为零），所以 `parseStringSection` 直接把整张表的 `stringData` 与 `stringOffsets` 设进 `EncodingReader`，供所有读取器共享（`inheritStringTableFrom`）。其余三张表都遵循惰性模式。

#### 4.3.2 核心流程

以 `LazyTypeTable` 为例（Constant 与 Debug 同构）：

```
parseTypeSection(payload):                          # 第一阶段：只搭骨架
    numTypes = readVarInt()
    skipPadding(4)
    typeStartIndices = reinterpret_cast<uint32_t*>(当前指针)   # 直接复用内存里的偏移数组
    skip(numTypes * 4)
    typeData = payload 剩余部分
    types.initialize(typeData, typeStartIndices, version)      # 记下数据区与偏移，不解析

LazyTypeTable.getType(index):                        # 第二阶段：按需解析
    if 已缓存: return cache[index]
    if index 正在解析中: return null                 # 递归保护
    标记 index 为「正在解析」
    start, end = typeStartIndices[index], 下一个偏移/payload.size
    typeReader = EncodingReader(typeData[start:end])
    typeTag = typeReader.readVarInt()
    parseTypeImpl(typeTag, payload)                  # 由 TypeBytecodeReader.inc 生成的 switch 分派
    缓存并返回
```

**惰性 + 直接 `reinterpret_cast`**：偏移数组是定长小端整数（Type 用 `uint32_t`、Constant 用 `uint64_t`、Debug 的两层用 `uint32_t` 与 `uint64_t`），由于写入端保证了对齐（Type/String 对齐 4，Constant/Debug 对齐 8），读取端可以**直接把原始缓冲 `reinterpret_cast` 成 `const uint32_t*` / `const uint64_t*`**，零拷贝得到偏移数组。这是对齐填充带来的直接红利。

#### 4.3.3 源码精读

**String 段**——最简单，搭好共享字符串表即可：

[lib/Bytecode/Reader/BytecodeReader.cpp:384-426](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L384-L426) —— 读 `numStrings`、4 字节对齐、把偏移数组直接 `reinterpret_cast` 成 `const uint32_t*`，剩余字节当 `stringData`，最后 `reader.setStringTable(...)` 注入主 reader。

[lib/Bytecode/Reader/BytecodeReader.cpp:271-274](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L271-L274) —— `inheritStringTableFrom`：子 reader（函数体、各 section）从主 reader 继承字符串表，避免每段重复解析。

**Type 段与 `LazyTypeTable`**——惰性解析的范本：

[lib/Bytecode/Reader/BytecodeReader.cpp:493-528](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L493-L528) —— `getType(index)`：缓存命中直接返回；用 `currentlyParsing` 集合做递归保护（配合 `llvm::scope_exit` 在退出时移除标记）；按偏移切出该类型的字节切片，读 typeTag，交 `parseTypeImpl` 解析，最后写缓存。

[lib/Bytecode/Reader/BytecodeReader.cpp:591-597](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L591-L597) —— `parseTypeImpl` 体内 `#include "TypeBytecodeReader.inc"` 的 `GEN_TYPE_READER_DISPATCH`，由 tblgen 生成一个按 typeTag 分派的 switch，绝大多数具体类型（Tile/指针/Token/各视图）的解析都由它自动完成。

[lib/Bytecode/Reader/BytecodeReader.cpp:558-589](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L558-L589) —— `FunctionType` 是少数仍手写的类型：读入参数/结果个数后，逐个用 `readAndGetType`（即「读一个类型索引再 `getType`」）递归解析，这正是需要递归保护的原因。

[lib/Bytecode/Reader/BytecodeReader.cpp:609-648](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L609-L648) —— `parseTypeSection`：搭骨架——读 `numTypes`、4 字节对齐、直接 `reinterpret_cast` 偏移数组、把数据区与偏移交给 `types.initialize`。

**Constant 段与 `DenseElementsAttrCache`**——常量去重缓存：

[lib/Bytecode/Reader/BytecodeReader.cpp:664-719](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L664-L719) —— `DenseElementsAttrCache::getOrCreate(type, dataBlob)`：以 `{type, dataBlob}` 为键去重；要求 type 是整/浮点 `TileType`，从 blob 里读出 `rawDataSize` 与原始字节，用 `DenseElementsAttr::isValidRawBuffer` 校验后 `getFromRawBuffer` 构造（大端机器还需做字节序转换）。

[lib/Bytecode/Reader/BytecodeReader.cpp:727-775](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L727-L775) —— `parseConstantSection`：8 字节对齐、`reinterpret_cast` 成 `const uint64_t*` 偏移数组，把每个常量切成一个 `ArrayRef<uint8_t>` 存进 `constants` 向量（仍不构造属性，留给 `DenseElementsAttrCache` 按需构造）。

**Debug 段与 `DebugInfoReader`**——两层索引 + 惰性 + 递归保护：

[lib/Bytecode/Reader/BytecodeReader.cpp:870-909](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L870-L909) —— `getDebugInfo(diIndex)`：与 `LazyTypeTable::getType` 同构——缓存、递归保护（`currentlyParsing` + `scope_exit`）、按偏移切片、读 `diTag`、交 `parseDebugInfo` 分派。

[lib/Bytecode/Reader/BytecodeReader.cpp:1085-1105](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L1085-L1105) —— `parseDebugInfo` 按 `DebugTag`（见 [BytecodeEnums.h:50-58](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h#L50-L58)）分派到 `parseDIFile`/`parseDISubprogram`/`parseDILexicalBlock`/`parseDILoc` 等，未知 tag 退化为 `UnknownLoc`。

[lib/Bytecode/Reader/BytecodeReader.cpp:1901-1987](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L1901-L1987) —— `parseDebugSection`：搭骨架——读两层索引（`diIndexOffsets[uint32]` 给每条操作一个起点、`diIndices[uint64]` 是真正的索引序列）与调试属性偏移 `diOffsets`，全部交给 `debuginfo.initialize`。两阶段对齐（先 4 后 8）。

#### 4.3.4 代码实践

**实践目标**：理解「偏移数组直接 `reinterpret_cast`」依赖对齐保证，并通过源码确认三张表的惰性入口。

**操作步骤**：

1.（源码阅读型）在 `parseTypeSection`（609–648 行）中找到 `reinterpret_cast<const uint32_t *>(reader.getCurrentPtr())`，确认它紧接在 `skipPadding(alignof(uint32_t))` 之后——即「4 字节对齐保证后，才能安全地把缓冲当 `uint32_t` 数组」。
2. 同样在 `parseConstantSection`（727–775 行）确认 Constant 段对齐的是 `alignof(uint64_t)`=8。
3. 追踪一次「类型被引用」的路径：函数签名在 `createFunction` 里通过 `types.getType(funcInfo.signatureIndex)` 触发（见 4.4.3），这正是惰性解析真正发生的地方。在 `LazyTypeTable::getType`（493–528 行）里确认：只有这第一次访问会真正解析，后续访问命中缓存。
4.（可选，需构建）构造一段含 `tile<4x8xf32>` 类型并 round-trip，用 `--mlir-print-debuginfo` 观察调试信息是否被正确恢复。

**需要观察的现象**：第 1、2 步确认每张表的「偏移数组 reinterpret_cast」都 preceded by 对应的 `skipPadding`；第 3 步确认 `getType` 的缓存与递归保护逻辑。

**预期结果**：能口述「对齐是 reinterpret_cast 的前提、惰性解析由首次索引引用触发」。

> 若本地未构建，第 4 步标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LazyTypeTable::getType` 需要 `currentlyParsing` 这个集合？

**参考答案**：因为类型可递归——函数类型的参数/结果通过索引引用其它类型，链路可能（直接或间接）引用回自身。若没有递归保护，遇到自引用类型会无限递归栈溢出。`currentlyParsing` 在进入某个索引时标记、用 `scope_exit` 退出时清除，发现「正在解析自己」就返回 null，打破环。

**练习 2**：String 段为什么不像 Type 段那样「惰性」？

**参考答案**：字符串本身没有内部结构、也没有跨条目引用，构造代价就是一次 `substr`，没有「省下无用构造」的收益；而且字符串表被几乎所有其它段频繁引用，惰性反而增加一次间接。所以它选择「一次性整张注入 `EncodingReader`，全员共享」。

---

### 4.4 函数体与全局的重建：createFunction / createGlobal / parseOperation

#### 4.4.1 概念说明

前三张表解析完，读取器掌握的是「元数据」：函数叫什么、签名是哪个类型、全局的初值是哪个常量。本模块讲如何把这些元数据**实例化成真实的 MLIR 操作**，以及函数体内部的每一条指令如何被 `InstructionParser` 逐条恢复。

三类顶层对象：

- **函数**：解析阶段在 `parseFunctionTableSection` 里把每个函数的元数据（名字索引、签名索引、entry 标志、可选优化提示、函数体原始字节）收进 `FunctionInfo` 列表；实例化阶段 `createFunction` 取出 `FunctionInfo`，创建 `cuda_tile::EntryOp`，再交给 `parseFunctionBody` 逐指令填充函数体。
- **全局**：`parseGlobalSection` 收集 `GlobalInfo`（符号名索引、类型索引、常量索引、对齐、可见性、是否常量），`createGlobal` 取出后用常量缓存构造初值 `DenseTypedElementsAttr`，创建 `cuda_tile::GlobalOp`。
- **指令**：`InstructionParser::parseOperation` 是函数体反序列化的核心——读 opcode、做版本可用性检查、取该操作的调试位置，然后由生成的 switch（`BytecodeReader.inc`）分派到具体的 `parse<Op>` 把操作数、结果类型、属性、region 都读出来并 `builder.create`。

指令解析有一个贯穿始终的设计：**`valueIndexList`**。字节码里，操作数与结果都用「索引」表示——每个 SSA 值在反序列化时被按生产顺序压入一个全局可见的 `valueIndexList`，后续操作引用操作数时给出索引，读取器从列表里取出对应的 `Value`。块（`parseBlock`）在进入时把块参数压入列表，退出时把列表**缩回**原大小，从而模拟 SSA 的作用域。

#### 4.4.2 核心流程

**函数重建**：

```
parseFunctionTableSection(payload):                  # 收集元数据
    numFunctions = readVarInt()
    for each function:
        nameIndex, signatureIndex, entryFlag, functionLocIndex = 连续读
        if 是 entry 且带优化提示: parseSelfContainedOpAttribute(...)  # 读 OptimizationHintsAttr
        lengthOfFunction = readVarInt()
        functionBody = readBytes(lengthOfFunction)   # 函数体原始字节先存着
        functionInfoList.append(...)

createFunction(funcInfo, builder):                    # 实例化
    funcName = stringTable[nameIndex]
    funcType = types.getType(signatureIndex)          # 惰性触发类型解析
    funcLoc  = debuginfo.getIterator(functionLocIndex).next()
    EntryOp::create(funcBuilder, funcLoc, name, type, argAttrs, retAttrs, optHints)
    entryBlock = funcOp.addEntryBlock()
    valueIndexList = entryBlock 的全部参数             # 函数参数进值表
    parseFunctionBody(functionBody, innerBuilder, valueIndexList, diIterator, ...)
```

**函数体逐指令**：

```
parseFunctionBody(bodyBytes, builder, valueIndexList, diIterator, ...):
    bodyReader = EncodingReader(bodyBytes)
    bodyReader.inheritStringTableFrom(mainReader)     # 继承字符串表
    while bodyReader 还有字节:
        InstructionParser::parseOperation(bodyReader, builder, valueIndexList, ...)

parseOperation(reader, builder, valueIndexList, ...):
    opcode = reader.readVarInt()
    if not isOpcodeAvailableInVersion(opcode, bytecodeVersion):   # 版本可用性检查
        error("unsupported opcode ... for bytecode version ...")
    loc = diIterator.next<LocationAttr>()              # 取本操作的调试位置
    # GEN_OP_READER_DISPATCH：生成的 switch 分派到 parse<Op>
    #   每个 parse<Op> 用 parseOperands 读操作数、按需读属性/region，
    #   最后 createOperationGeneric(...) 把结果压入 valueIndexList
```

**全局重建**：

```
createGlobal(globalInfo, builder):
    symName  = stringTable[symbolNameIndex]
    valueAttr = constCache.getOrCreate(types.getType(valueTypeIndex),
                                       constants[constantValueIndex])  # 惰性
    visibility = symbolizeSymbolVisibility(symbolVisibility)
    GlobalOp::create(builder, UnknownLoc, symName, denseValueAttr,
                     alignment, constantAttr, visibilityAttr)
```

#### 4.4.3 源码精读

**函数段**——`FunctionInfo` 元数据结构、收集与实例化：

[lib/Bytecode/Reader/BytecodeReader.cpp:2008-2016](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2008-L2016) —— `FunctionInfo` 字段：`nameIndex / signatureIndex / functionLocIndex / entryFlag / lengthOfFunction / functionBody / optimizationHints`。

[lib/Bytecode/Reader/BytecodeReader.cpp:2020-2101](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2020-L2101) —— `parseFunctionTableSection`：逐函数读名字/签名/entry 标志/位置索引，entry 且置了 `HasOptimizationHints` 位才读优化提示（见 [FunctionFlags](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h#L66-L73)），再读函数体长度并切出原始字节存进 `functionBody`。

[lib/Bytecode/Reader/BytecodeReader.cpp:2125-2218](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2125-L2218) —— `createFunction`：从字符串表取名字、惰性取签名类型（必须能 `dyn_cast` 成 `FunctionType`）、取函数位置；按 entry 标志创建 `EntryOp`（非 entry 直接报「un-expected non-entry function」，说明当前只支持 entry），把入口块参数压入 `valueIndexList`，最后调 `parseFunctionBody`。

[lib/Bytecode/Reader/BytecodeReader.cpp:2104-2122](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2104-L2122) —— `parseFunctionBody`：构造子 reader、继承字符串表、循环 `parseOperation` 直到字节耗尽。

**指令解析**——`InstructionParser` 的核心：

[lib/Bytecode/Reader/BytecodeReader.cpp:1851-1881](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L1851-L1881) —— `parseOperation`：读 opcode；调 `isOpcodeAvailableInVersion`（见 [Version.cpp:70-77](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L70-L77)，按 `(major,minor)` 查「该版本支持的最大 opcode」，opcode 超出即不支持）做版本可用性检查；从 `diIterator` 取本操作的位置；最后 `#include "BytecodeReader.inc"` 的 `GEN_OP_READER_DISPATCH` 生成 switch 分派到各 `parse<Op>`。

[lib/Bytecode/Reader/BytecodeReader.cpp:1169-1193](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L1169-L1193) —— `parseOperands`：读操作数个数与各操作数索引，从 `valueIndexList` 取出对应 `Value`，越界即报错。

[lib/Bytecode/Reader/BytecodeReader.cpp:1138-1164](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L1138-L1164) —— `createOperationGeneric`：用 `OperationState` 装配操作数/结果类型/属性/region，`builder.create`，再把结果按 `numResultsForValueIndex` 压入 `valueIndexList`（该参数用于老字节码的向后兼容，控制把多少个结果入表）。

[lib/Bytecode/Reader/BytecodeReader.cpp:1196-1260](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L1196-L1260) —— `parseBlock`：读块参数个数与各参数类型（惰性取类型）、压入 `valueIndexList`；读操作数循环调 `parseOperation`；末尾校验块必须有终止符；退出时把列表 `resize` 回原大小，模拟 SSA 作用域。

[lib/Bytecode/Reader/BytecodeReader.cpp:1263-1289](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L1263-L1289) —— `parseRegion`：读块个数，逐块 `emplaceBlock` 并 `parseBlock`。

**全局段**——`GlobalInfo`、收集（含版本分支）与实例化：

[lib/Bytecode/Reader/BytecodeReader.cpp:2238-2245](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2238-L2245) —— `GlobalInfo` 字段。

[lib/Bytecode/Reader/BytecodeReader.cpp:2249-2323](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2249-L2323) —— `parseGlobalSection`：注意版本分支——13.3 起每个 global 多了 `symbolVisibility` 与 `constant` 两个字段（`kMinGlobalInfoSize` 在 13.3+ 为 6、之前为 4），老版本用默认值（Public / 非常量）。这是 `sinceVersion` 在读取端的直接体现。

[lib/Bytecode/Reader/BytecodeReader.cpp:2353-2408](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2353-L2408) —— `createGlobal`：用常量缓存 + 类型表构造初值（必须能 `dyn_cast` 成 `DenseTypedElementsAttr`），符号化可见性，全局变量位置强制为 `UnknownLoc`（CudaTile 只支持 local scope 的 `DILocAttr`，全局不能用），最后 `GlobalOp::create`。

**Producer 段**（最简单的一个段，仅一个字符串索引）：

[lib/Bytecode/Reader/BytecodeReader.cpp:2332-2350](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L2332-L2350) —— 读一个字符串索引，取出的 producer 字符串最终挂到 `ModuleOp` 上。

#### 4.4.4 代码实践

**实践目标**：跟踪一次「函数签名类型」的惰性解析，并确认 `valueIndexList` 如何在块入口/出口维护作用域。

**操作步骤**：

1.（源码阅读型）从 `createFunction`（2125–2218 行）出发，找到 `types.getType(funcInfo.signatureIndex)`（约 2144 行）——这是「函数签名类型」首次被真正解析的触发点。回溯到 `LazyTypeTable::getType`（493–528 行），确认解析结果被缓存。
2. 在 `parseBlock`（1196–1260 行）中找到两处对 `valueIndexList` 的操作：入口处 `addArgument` 后 `push_back`（块参数入表）、`originalValueIndexListSize = valueIndexList.size()`（记录基线）；各操作的 `parseOperation` 把结果入表；出口处 `valueIndexList.resize(originalValueIndexListSize)`（收回作用域）。画出这条「值的生产—消费—收回」链路。
3. 在 `parseGlobalSection`（2249–2323 行）中找到 `bytecodeVersion >= *version_13_3` 的两处分支，说明 13.3 之前的字节码为何也能被读取（用默认可见性/非常量）。
4.（可选，需构建）对一段含一个 entry 与一个 global 的 MLIR 做 round-trip，用 `cuda-tile-translate -cudatilebc-to-mlir <f.tilebc>` 观察反序列化后的文本，确认函数与全局都被还原。

**需要观察的现象**：第 1 步看到惰性解析发生在「签名被引用」时；第 2 步看到块作用域由 `resize` 模拟；第 3 步看到版本分支用默认值兜底老格式。

**预期结果**：能用一张图说清「函数元数据→惰性取签名类型→创建 EntryOp→逐指令填函数体（值表驱动）」的完整链路。

> 若本地未构建，第 4 步标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`parseOperation` 里 `isOpcodeAvailableInVersion` 检查失败时，为什么是「不可恢复的错误」而不是「跳过该指令」？

**参考答案**：字节码是定长/变长混合的紧凑格式，opcode 决定了后续操作数/属性/region 的读法。一旦遇到当前版本不支持的 opcode，读取器无法知道这条指令占多少字节、如何分派，也就无法安全地「跳过」它继续——剩余字节的对齐会全部错位。所以只能报错终止整个反序列化。

**练习 2**：`createGlobal` 为什么强制给全局变量用 `UnknownLoc`？

**参考答案**：CudaTile 的调试位置校验（见 u6-l3）要求 `DILocAttr` 的 scope 必须是 local scope（子程序/词法块），而全局变量不属于任何函数体，没有合法的 local scope 可挂。因此规约上全局变量只能用 `UnknownLoc`，读取器直接硬编码这一选择。

**练习 3**：`parseGlobalSection` 是如何做到「同一份代码同时读 13.1 与 13.3 的 global 段」的？

**参考答案**：它用 `bytecodeVersion >= version_13_3` 判断，13.3+ 多读 `symbolVisibility` 与 `constant` 两个字段，老版本则用默认值（Public / 非常量）。这种「按版本条件读额外字段、否则填默认」的写法，就是 `sinceVersion` 标注在读取端的具体落地，实现前向兼容（新版读取器能读旧版字节码）。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**手工解码一个最小字节码文件的 header 与第一个 section 帧**。

1. 用 `cuda-tile-translate -mlir-to-cudatilebc` 把下面这段 MLIR 编译成 `mini.tilebc`（参考 u7-l1 的命令）：
   ```mlir
   cuda_tile.module @kernels {
     cuda_tile.entry @return_op(%a: !cuda_tile.tile<i32>) {
       cuda_tile.return
     }
   }
   ```
2. 用十六进制工具查看 `mini.tilebc` 的前若干字节（如 `od -An -tu1 mini.tilebc | head`）。
3. **手工标注**：
   - 第 0–7 字节：应为 magic `127 84 105 108 101 73 82 0`（`0x7F 'T' 'i' 'l' 'e' 'I' 'R' 0x00`）；
   - 第 8–11 字节：版本 `verMajor verMinor tagLo tagHi`，应解出 13.3.0（或你指定的 `--bytecode-version`）；
   - 第 12 字节起：第一个 section 帧——拆出低 7 位 sectionID、高位对齐标志，对照 [Section 枚举](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h#L30-L42) 判断它是哪一段（很可能是 String 段，ID=1）。
4. 把你的标注与源码里的 `parseHeader`（BytecodeReader.cpp:299-328）、`parseSectionHeader`（BytecodeReader.cpp:331-372）逐字段对照，确认每一处取值都解释得通。
5. 进阶：尝试用编辑器改动一个字节（例如把 magic 首字节从 `0x7F` 改成 `0x7E`），重新跑 `cuda-tile-translate -cudatilebc-to-mlir mini.tilebc`，确认得到与 `invalid_magic_number.tileirbc` 一致的 `invalid magic number` 报错。

**预期结果**：能独立从原始字节读出 magic、版本、第一个 section 的 ID 与对齐标志，并把每个字段对应到读取器的具体源码行。若本地无法构建，则完成「字节→源码行」的静态对照，并标注「待本地验证」。

## 6. 本讲小结

- `readBytecode` 是反序列化总入口，采用「**先发现、后解析**」的两阶段设计：先线性扫描把所有 section 的 payload 按 ID 存起来，再按依赖顺序（String→Producer→Type→Constant→Global→Func→Debug）解析，因此对文件内 section 的物理顺序鲁棒；String 与 Func 两段必填。
- `EncodingReader` 是整份读取器的解码底座，提供 varint、有符号 varint（zigzag）、`skipPadding` 与带偏移的 `emitError`；`parseHeader` 逐字节校验 magic 与版本，`parseSectionHeader` 拆出 sectionID（低 7 位）/对齐标志（高位）/length/alignment。
- String/Type/Constant/Debug 四张表结构一致（偏移数组 + 数据区），读取端普遍采用「**惰性解析 + 缓存 + 递归保护**」：解析段时只搭骨架（偏移数组甚至直接 `reinterpret_cast`，依赖写入端的对齐保证），条目被索引引用时才真正构造并缓存。
- 函数重建分两步：`parseFunctionTableSection` 把每个函数的元数据（含函数体原始字节）收进 `FunctionInfo`，`createFunction` 惰性取签名类型、创建 `EntryOp`，再由 `parseFunctionBody` 逐指令填充；全局同理（`GlobalInfo` → `createGlobal`），且 `parseGlobalSection` 用版本分支兼容 13.1/13.3 两种字段数。
- `InstructionParser::parseOperation` 是函数体反序列化核心：读 opcode → `isOpcodeAvailableInVersion` 版本检查 → 从 `diIterator` 取调试位置 → 生成的 switch 分派到 `parse<Op>`；操作数/结果靠 `valueIndexList` 索引驱动，块作用域由入口压参数、出口 `resize` 模拟。
- 读取端的错误路径密集且信息友好：非法 magic、不支持版本、未知 section ID、EndOfBytecode 非末段、opcode 超版本等都有明确报错，`test/Bytecode/invalid/` 提供了对应的回归用例。

## 7. 下一步学习建议

- 阅读 [include/cuda_tile/Bytecode/Common/Version.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/Version.h) 与 [lib/Bytecode/Common/Version.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp)，配合 u7-l4 系统理解「最低/兼容/当前版本」与 `sinceVersion` 如何同时约束写入端与读取端，构成前后向兼容策略。
- 进入 u8-l1（cuda-tile-tblgen 字节码代码生成），看清本讲反复 `#include` 的 `BytecodeReader.inc` / `TypeBytecodeReader.inc` / `AttrBytecode.inc` 是如何从 `.td` 自动生成的，从而理解「同一份 `.td` 同时驱动写入胶水与读取胶水」的单一数据源设计。
- 对照阅读 u7-l2（写入器），把本讲的 `LazyTypeTable`/`DebugInfoReader`/`InstructionParser` 与写入端的 `TypeManager`/`DebugInfoWriter`/`FunctionTableWriter` 一一对应，建立「序列化—反序列化」的完整心智模型。
- 想加深对读取鲁棒性的理解，可继续浏览 `test/Bytecode/invalid/` 下其余用例（`invalid_dense_map_value.bc`、`invalid_attribute_name.bc`、`invalid_loc.mlir`、`invalid_not_self_contained.mlir`），以及 `test/Bytecode/versioning/` 目录里的前后向兼容测试。
