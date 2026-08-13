# dump bin 文件 TLV 格式解析

## 1. 本讲目标

上一讲（u7-l1）我们已经搞清楚 dump bin 文件**从哪里来**：算子侧调用 `AscendC::DumpTensor` / `printf` / `PrintTimeStamp`，再由 CANN 运行时按 `acl.json` 的 `dump_kernel_data` 配置把调试数据落盘成 `.bin` 文件。本讲要回答的是下一个自然的问题：**这些 bin 文件里到底是什么？** `show_kernel_debug_data` 工具凭什么能把一串二进制字节还原成可读的张量值、格式化字符串和时间戳？

学完本讲你应当能够：

- 说清 dump bin 采用的 **TLV（Type-Length-Value）二进制布局**，以及为什么这种布局适合做调试数据的容器。
- 区分 **FIFO** 与 **workspace（legacy）** 两种 bin 文件的整体结构，理解各自的"信封"——`FifoBlockInfo` 与 `BlockInfo`。
- 解释工具如何**只用文件开头 4 个字节**（magic）就把文件分派到 `FifoDumpBinFile` 还是 `DumpBinFile`，即 `parse_dump_bin` 的分发逻辑。
- 具备手工构造一个最小 bin 文件并预测解析结果的能力。

## 2. 前置知识

阅读本讲前，建议先建立以下概念（部分来自前置讲义）：

- **bin 文件**：kernel 侧调试信息落盘后的二进制产物，扩展名 `.bin`，内容由 CANN 运行时写定，asc-tools 只负责离线解析。
- **核（core）/ block**：NPU 上的执行单元。dump 数据按核组织，每个核的数据在文件里占据一个区块（block），所以 bin 文件天然是"多核 → 多块"的结构。
- **AIC / AIV / SIMT**：Ascend 的核类型。AIC 偏 Cube（矩阵）计算，AIV 偏 Vector（向量）计算，SIMT 是细粒度的线程级并行核（见 u3-l2）。
- **Python `struct` 模块**：本讲大量出现 `struct.pack("II", ...)`、`struct.unpack("iiiiiiQ", ...)` 这样的格式串。`I` = 无符号 32 位整数（4 字节），`i` = 有符号 32 位，`H` = 无符号 16 位（2 字节），`Q` = 无符号 64 位（8 字节），`f`/`d` = 单/双精度浮点。`struct.calcsize("II")` 返回该格式串占用的字节数。
- **TLV（Type-Length-Value）**：一种"自描述"的二进制编码：先写类型，再写长度，最后写字节流。读出长度后就能精确跳到下一条记录，无需事先约定字段顺序。

> 提示：本讲几乎不涉及 kernel 侧代码，聚焦于 `utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py` 与 `data_converter.py` 这两个解析端文件。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [dump_parser.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py) | 解析核心。定义 TLV、各类 Header、`BlockInfo`/`FifoBlockInfo`、`DumpBinFile`/`FifoDumpBinFile`、以及顶层分发函数 `parse_dump_bin`。 |
| [data_converter.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/data_converter.py) | 仅含 `decode_bfloat16`，把 bf16 的 16 位比特还原成 Python float，作为 TLV 值解码的"特殊转换器"。 |
| [test_show_kernel_debug_data.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py) | 单元测试。它用 `struct.pack` **手工拼装**出真实的 bin 字节流，是理解二进制格式最权威的"样例数据"，本讲多次借用。 |

解析端整体分层如下：

```
parse_dump_bin(bin 文件)        ← 顶层入口：读 magic，决定走哪条流程
   │
   ├── magic 低16位 == 0xAE86  → FifoDumpBinFile   (FIFO 文件，单核一份)
   └── magic      == 0x5AA5BCCD → DumpBinFile      (workspace 文件，多核拼一份)
              │
              ▼
        读取 BlockInfo/FifoBlockInfo（"信封"）
              │
              ▼
        循环读取 TLV（"信纸"）→ 按 tag 分发到 DumpTensor / PrintStruct / ...
```

本讲按"信纸 → 信封 → 分发"的顺序展开，对应三个最小模块。

## 4. 核心概念与源码讲解

### 4.1 TLV 结构：dump 文件的最小寻址单元

#### 4.1.1 概念说明

无论 FIFO 还是 workspace 文件，文件里真正承载调试内容的"一条记录"都是 **TLV**。一条 TLV 由三段组成：

- **Type（tag）**：一个整数，说明这条记录是什么类型（张量？printf？时间戳？）。
- **Length**：紧跟其后的 Value 占多少字节。
- **Value**：长度为 Length 的原始字节流，具体含义由 Type 决定。

TLV 的最大好处是**自描述 + 可顺序扫描**：读取一个 `Type+Length` 头后，不必理解 Value 内部细节，只要按 Length 跳过，就能定位到下一条记录。这使得解析器可以"先按格式切分，再按类型解读"，两步解耦。

#### 4.1.2 核心流程

读取一条 TLV 的过程：

```
1. 从文件读 8 字节头部（两个 uint32：tag、length）
2. 再读 length 字节作为 value
3. 这条 TLV 总字节数 = 8 + length
4. 文件游标自然前进到下一条 TLV 起点
```

TLV 头部固定 8 字节（两个 4 字节无符号整数），因此解析器在循环里反复"读头 → 读值"，直到本块数据耗尽。这与 u6 讲过的 Kernel ELF 里的 `.ascend.meta` TLV（2 字节头）思想相同，只是这里的头部更宽（4+4 字节），因为 Type 取值范围很大（见 4.1.3 的 `SIMT_PRINTF_TYPE = 0xF0E00F0E`）。

#### 4.1.3 源码精读

`TLV` 用一个 dataclass 承载，关键在格式串 [`get_tl_format`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L87-L90) 返回 `"II"`——即两个无符号 32 位整数，共 8 字节：

```python
@classmethod
def get_tl_format(cls):
    # TLV header uses uint32_t type/length.
    return "II"
```

读取逻辑在 [`TLV.read`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L70-L74)：

```python
def read(self, f):
    tl_fmt = self.get_tl_format()
    tl_size = self.get_tl_size()          # struct.calcsize("II") == 8
    self.tag, self.length = struct.unpack(tl_fmt, f.read(tl_size))
    self.value = f.read(self.length)      # 再读 length 字节
```

Type 字段的所有合法取值定义在 [`DumpType`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L904-L913) 枚举里：

```python
class DumpType(Enum):
    DEFAULT_TYPE = 0
    SCALAR_TYPE = 1          # printf / PRINTF
    TENSOR_TYPE = 2          # DumpTensor
    SHAPE_TYPE = 3           # 张量形状（紧跟 TENSOR 之前，描述下面的张量）
    ASSERT_TYPE = 4          # ascend_assert
    META_TYPE = 5            # 元信息（核数、核类型）
    TIME_STAMP = 6           # PrintTimeStamp
    SIMT_PRINTF_TYPE = 0xF0E00F0E   # SIMT 核的 printf，取值很大，故头用 uint32
    SIMT_ASSERT_TYPE = 0xF0F00F0F
```

注意 `SIMT_PRINTF_TYPE` 的值高达 `0xF0E00F0E`，远超 16 位能表示的范围——这正是 TLV 头部选 4 字节 `uint32` 而非 2 字节的原因。

读到一条 TLV 后，由 [`DumpCoreContent.add_tlv_data`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L745-L783) 按 `tag` 二次分发：`TENSOR_TYPE` 解析成 `DumpTensor`、`SCALAR_TYPE`/`ASSERT_TYPE` 解析成 `PrintStruct`、`SHAPE_TYPE` 解析成 `ShapeInfo`、`TIME_STAMP` 解析成 `TimeStampInfo`。这就是"先切分、后解读"的落地：TLV 只负责切出一条条记录，`add_tlv_data` 才决定每条记录怎么还原。

#### 4.1.4 代码实践

**实践目标**：用 `struct` 手工拼一条 TLV，验证"读头 → 读值"能正确还原。

**操作步骤**（以下为示例代码，可在任意 Python3 环境运行）：

```python
import struct, io
# 构造一条 tag=2 (TENSOR_TYPE), value=b"hello" 的 TLV
raw = struct.pack("II", 2, 5) + b"hello"
# 模拟 dump_parser.TLV.read
buf = io.BytesIO(raw)
tl_fmt = "II"
tag, length = struct.unpack(tl_fmt, buf.read(struct.calcsize(tl_fmt)))
value = buf.read(length)
print(tag, length, value)
```

**需要观察的现象**：`tag` 与 `length` 经 `struct.calcsize("II") == 8` 字节头部还原，`value` 恰好是 5 字节。

**预期结果**：输出 `2 5 b'hello'`。这正是单元测试 [`test_read`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L237-L248) 所断言的行为（它用 `struct.pack("ii", 2, 5) + b"hello"`，有符号/无符号在此值下结果一致）。若你的 Python 环境正常，结果应稳定可复现；如遇字节序疑问，可在本机执行 `python3 -c "import struct; print(struct.calcsize('II'))"` 确认头部为 8。

#### 4.1.5 小练习与答案

**练习 1**：为什么 TLV 头部用 `"II"`（两个 `uint32`）而不是 u6 讲的 `.ascend.meta` 那样的 2 字节头？

**参考答案**：dump 的 Type 取值范围大，`SIMT_PRINTF_TYPE = 0xF0E00F0E` 远超 16 位；同时 4 字节长度字段让单条 Value 可达 4GiB，足以装下大张量。`.ascend.meta` 的字段是少量预定义枚举，2 字节够用，且更省空间。

**练习 2**：如果一条 TLV 的 `length` 字段被损坏成超大值，解析器会怎样？

**参考答案**：会读到非法的 value（`f.read` 提前返回不足长度的字节）。FIFO 路径在 [`FifoDumpBinFile.parse`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L1080-L1088) 中有显式的 `length > file_size` 与 `length > remain_size` 双重溢出检查，会抛 `RuntimeError("FifoDumpBinFile: TLV length overflow")`（见测试 `test_invalid_tlv_length`）。

---

### 4.2 BlockInfo 与 Header：每核数据的"信封"

#### 4.2.1 概念说明

TLV 是"信纸"，但一个 bin 文件里有成百上千条 TLV，还可能来自多个核。解析器需要一种"信封"来描述：**这一块数据属于哪个核、一共多大、还剩多少未用、是不是合法的 dump 数据**。这个信封就是 `BlockInfo`（workspace 文件）或 `FifoBlockInfo`（FIFO 文件）。

两种信封对应两种文件形态：

- **workspace（legacy）文件**：多个核的数据**拼接**在同一个文件里，每个核占一个固定大小的块（默认 1MiB）。文件结构 = `[BlockInfo][TLV...][填充] [BlockInfo][TLV...][填充] ...`。
- **FIFO 文件**：每个核**单独**一个文件，文件结构 = `[FifoBlockInfo][TLV...][TLV...]`。文件名通常带核类型与核号，如 `asc_kernel_data_aiv_0.bin`。

之所以有两种形态，是因为硬件 dump 通道的演进：早期走 workspace 共享缓冲（一核一块、固定大小），新架构（如 950pr 的 SIMT/AIV）走按核独立的 FIFO 环形缓冲。解析端必须同时兼容两者。

#### 4.2.2 核心流程

**workspace 文件的逐块扫描**（伪代码）：

```
while 未到文件尾:
    读 32 字节 BlockInfo
    若 magic != 0x5AA5BCCD → 跳过 1MiB 继续（容错）
    core_dump_size = total_size - 32(header) - remain_size(填充)
    在 core_dump_size 范围内循环读 TLV
    文件游标跳到下一个块起点（按 total_size 对齐）
```

**FIFO 文件的扫描**更简单：

```
读 56 字节 FifoBlockInfo（并校验 magic == 0xAE86）
循环读 TLV 直到文件尾（带 length 溢出校验）
```

注意一个关键差异：workspace 的块大小由 `BlockInfo.total_size` 字段决定（默认 1MiB），扫描时按它跳步；FIFO 文件没有"块大小"概念，TLV 紧贴信封头连续排列直到 EOF。

#### 4.2.3 源码精读

先看两种信封的结构。workspace 的 [`BlockInfo`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L565-L614) 用格式串 `"iiiiiiQ"`，共 32 字节：

```python
@classmethod
def get_format(cls):
    return "iiiiiiQ"   # 6×int32 + 1×uint64 = 32 字节

def is_valid(self):
    block_info_magic = 0x5AA5BCCD
    return block_info_magic == self.magic_num
```

各字段含义：`total_size`（本块总字节数）、`block_id`（块号）、`block_num`（总块数）、`remain_size`（块内剩余未用字节）、`magic_num`（魔数 `0x5AA5BCCD`）、`reserved`（保留位，等于 7 时警告见下）、`dump_addr`（8 字节，dump 起始地址）。

FIFO 的 [`FifoBlockInfo`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L617-L659) 字段更丰富，格式串 `"IIIIHHIQ6I"`，共 56 字节：

```python
@classmethod
def get_format(cls):
    return "IIIIHHIQ6I"   # 4×u32 + 2×u16 + u32 + u64 + 6×u32

def is_valid(self):
    return self.magic == 0xAE86
```

它的 `magic` 字段是 16 位（`H`），值 `0xAE86`；此外多了 `core_id`、`flag`（核类型标志，见 4.3）等字段。

**重要细节：两种信封里 magic 字段都从文件偏移 16 处开始**。对照字节布局：

| 偏移 | BlockInfo（workspace） | FifoBlockInfo（FIFO） |
|------|------------------------|------------------------|
| 0–15  | total_size/block_id/block_num/remain_size（各 4B） | length/core_id/block_num/remain_len（各 4B） |
| **16–17** | **magic_num 低 2 字节** | **magic（0xAE86）** |
| 18–19 | magic_num 高 2 字节 | flag |
| 20–23 | magic_num 高位…（仍是 magic_num 的一部分） | rsv |
| …    | reserved, dump_addr(8B) | dump_addr(8B), resv(6×4B) |

这张表是 4.3 节 magic 分发的物理基础：两种信封虽然字段不同，但**魔数都写在偏移 16**，所以读同一窗口的 4 字节就能同时探测两者。

再看实际扫描循环。workspace 在 [`get_dump_core_contents`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L992-L1001) 里嵌套读 TLV：

```python
core_dump_size = block_info.total_size - block_info_size - block_info.remain_size
while tlv_offset < core_dump_size:
    tlv = TLV()
    tlv.read(bin_file)
    core_content.add_tlv_data(tlv)
    tlv_offset += tlv.get_size()
```

注意 `core_dump_size` 的算法：块总大小减去信封头（32B）、再减去尾部填充（remain_size），剩下的才是真正的 TLV 区。读到超出这个范围就停止本块。`total_block_size` 初值是 `ONE_MEGA_BYTE = 1024*1024`（[第 28 行](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L28)），读到合法 BlockInfo 后才更新为实际 `total_size`。

FIFO 在 [`FifoDumpBinFile.parse`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L1058-L1104) 里单核直读，并对每条 TLV 做了比 workspace 更严格的安全校验：

```python
if tlv.length > file_size:                        # 防御 1：超过文件总大小
    raise RuntimeError(f"FifoDumpBinFile: TLV length overflow, length={tlv.length}")
remain_size = file_size - bin_file.tell()
if tlv.length > remain_size:                      # 防御 2：超过剩余可读字节
    raise RuntimeError(f"FifoDumpBinFile: TLV length overflow, ...")
```

> 补充：`DumpTensor` 的 Value 内部还有一层"小信封"——[`DumpMessageHeader`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L100-L139)，格式串 `"iiiiii"`（24 字节，含 addr/data_type/desc/buffer_id/position/reserved）。也就是说一条 `TENSOR_TYPE` 的 TLV，其 Value = `DumpMessageHeader(24B)` + 真正的张量字节。其中 `data_type` 决定如何按 `dtype_to_fmt` 表把字节还原成数值，`desc` 即 `DumpTensor(tensor, desc, ...)` 的第二个参数（u7-l1 提到的 index）。这一层的逐类型解析（printf/tensor/timestamp）留给 u7-l3。

#### 4.2.4 代码实践

**实践目标**：参照单元测试，手工拼一个合法的 workspace `BlockInfo` 头，并验证 `is_valid()`。

**操作步骤**（示例代码，直接取自测试 [`test_unpack_block_info`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L565-L571) 的构造方式）：

```python
import struct
# 字段: total_size, block_id, block_num, remain_size, magic_num, reserved, dump_addr
buf = struct.pack("iiiiiiQ", 1024*1024, 0, 1, 100, 1520811213, 0, 32768)
print("字节长度:", len(buf))          # 32
magic = struct.unpack("iiiiiiQ", buf)[4]
print("magic_num:", hex(magic))        # 0x5aa5bccd
print("合法:", magic == 0x5AA5BCCD)    # True
```

**需要观察的现象**：`1520811213` 的十六进制正是 `0x5AA5BCCD`；`len(buf)` 恰好 32，印证信封头大小。

**预期结果**：输出 `字节长度: 32`、`magic_num: 0x5aa5bccd`、`合法: True`。FIFO 信封可仿照测试 [`test_unpack_fifo_block_info`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L593-L602) 用 `struct.pack("IIIIHHIQ6I", 256, 5, 1, 0, 44678, ...)`，其中 `44678 == 0xAE86`。本机可直接验证；字节序默认小端，与 NPU 落盘一致，无需额外设置。

#### 4.2.5 小练习与答案

**练习 1**：workspace 文件里，已知某块 `total_size=256`、`remain_size=200`，块内能容纳多少字节的 TLV 数据？

**参考答案**：`core_dump_size = total_size - block_info_size - remain_size = 256 - 32 - 200 = 24` 字节。测试 [`test_parse_with_valid_block`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L1417-L1427) 正是用这组参数构造的。

**练习 2**：为什么 `FifoDumpBinFile.parse` 要做两层 length 校验，而 workspace 的 `get_dump_core_contents` 看起来没有？

**参考答案**：workspace 用 `core_dump_size` 作为循环上界，天然把 TLV 读限制在块内（超出即停）；FIFO 文件没有"块大小"边界，TLV 紧贴到 EOF，若 length 字段损坏会一直读到非法内存，故需显式对照 `file_size` 与剩余字节做防御。

---

### 4.3 magic 分发：用前 20 字节识别 FIFO 与 workspace

#### 4.3.1 概念说明

既然存在两种文件形态，解析器面对一个陌生 bin 文件时，第一件事就是判断它属于哪一种。`show_kernel_debug_data` 的做法非常轻量：**只读文件开头偏移 16 处的 4 个字节**，就能在 `FifoDumpBinFile`（FIFO）与 `DumpBinFile`（workspace）之间做出选择。

这 4 字节就是"magic（魔数）"——一种约定好的、出现在固定位置的特征值，用来快速识别文件格式（类似 PNG 的 `89 50 4E 47`、Java class 的 `CAFEBABE`）。两种 dump 文件的 magic 不同，且恰好都落在偏移 16，于是只需一次探测即可分流。

#### 4.3.2 核心流程

`parse_dump_bin` 的分派逻辑可用如下流程图表达：

```
              读取文件偏移 [16:20] 的 4 字节作为 raw_magic (uint32)
                              │
                              ▼
              ┌─── raw_magic 为 None（文件 <20 字节）? ───┐
              │是                                         │否
              ▼                                           ▼
        走 workspace 分支                  ┌── (raw_magic & 0xFFFF) == 0xAE86 ? ──┐
        (DumpBinFile)                      │是                                    │否
                                            ▼                                      ▼
                                   FIFO 分支 (FifoDumpBinFile)         ┌── raw_magic == 0x5AA5BCCD ? ──┐
                                                                        │是                              │否
                                                                        ▼                                ▼
                                                                 workspace 分支            raise RuntimeError
                                                                 (DumpBinFile)             "unknown block magic"
```

对应的判定条件（摘自 [`parse_dump_bin`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L1225-L1241)）：

```python
if raw_magic is not None and (raw_magic & 0xFFFF) == 0xAE86:
    ... dump_file = FifoDumpBinFile(dump_bin, core_type, core_id)
elif raw_magic is None or raw_magic == 0x5AA5BCCD:
    ... dump_file = DumpBinFile(dump_bin)
else:
    raise RuntimeError(f"unknown block magic: 0x{raw_magic:08X}")
```

判定顺序有讲究：**先判 FIFO 再判 workspace**。因为 FIFO 的 magic（`0xAE86`）是 16 位、只占偏移 16–17，而偏移 18–19 是 `flag` 字段。若反过来先做 workspace 的全 32 位相等比较，会把 `flag != 0` 的 FIFO 文件误判。所以用"低 16 位掩码"先抓 FIFO，剩下的才留给 workspace。

#### 4.3.3 源码精读

探测函数 [`_read_block_magic_core_id_and_flag`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L1171-L1182) 只读文件最前面的 `FifoBlockInfo.get_size()`（56）字节，并从偏移 16 处切 4 字节：

```python
def _read_block_magic_core_id_and_flag(dump_bin):
    raw_magic = None
    fifo_core_id = None
    fifo_flag = None
    with open(dump_bin, "rb") as bin_file:
        header = bin_file.read(FifoBlockInfo.get_size())   # 读 56 字节
        if len(header) >= 20:
            raw_magic = struct.unpack("I", header[16:20])[0]  # 关键：偏移16处的4字节
            unpacked = struct.unpack(FifoBlockInfo.get_format(), header)
            fifo_core_id = unpacked[1]
            fifo_flag = unpacked[5]
    return raw_magic, fifo_core_id, fifo_flag
```

为什么 `header[16:20]` 同时能取出两种文件的 magic？回顾 4.2.3 的字节布局表：

- **workspace**：偏移 16–19 正好是 `magic_num`（int32）本身，读出即 `0x5AA5BCCD`。
- **FIFO**：偏移 16–17 是 `magic`（0xAE86），18–19 是 `flag`；按 uint32 小端读取得到 `raw_magic = 0xAE86 | (flag << 16)`，所以 `(raw_magic & 0xFFFF) == 0xAE86` 成立。

这是一处"两种格式在同一字节窗口对齐"的精心设计（或巧合），让分发只需 4 字节。

FIFO 分支还需要决定 `core_type`（aic/aiv/simt）。优先看文件名：若文件名形如 `asc_kernel_data_<type>_<id>.bin`（[`is_asc_kernel_data`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L1214-L1220) 判定），就用文件名里的 type/id；否则回退到信封里的 `flag` 字段，经 [`_core_type_from_fifo_flag`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L1166-L1168) 映射：

```python
def _core_type_from_fifo_flag(flag):
    flag_to_core_type = {0: "aic", 1: "aiv", 2: "simt"}
    return flag_to_core_type.get(flag, "fifo")
```

> 一个对称的设计值得点出：本讲有**两级分发**。外层（文件级）按 magic 在 `FifoDumpBinFile`/`DumpBinFile` 间分流；内层（记录级）按 TLV 的 tag 在 `DumpTensor`/`PrintStruct`/`ShapeInfo`/`TimeStampInfo` 间分流（见 4.1.3 的 `add_tlv_data`）。magic 决定"信封怎么拆"，tag 决定"信纸怎么读"。

#### 4.3.4 代码实践

**实践目标**：画出根据 magic 值选择 `FifoDumpBinFile`/`DumpBinFile` 的判断流程图，并用真实构造的字节流验证每个分支。

**操作步骤**：

1. **画流程图**：把 4.3.2 的文字流程图誊抄/重绘成你习惯的形式（Mermaid、手绘均可），标注三个判定节点与四条出口（FIFO / workspace / workspace(过小报错) / unknown 报错）。

2. **构造三种文件并预测分支**（示例代码，沿用测试 [`_merged_ParseDumpBin__build_fifo_block_info`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L1460-L1477) 与 [`test_parse_unknown_magic`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L1559-L1568) 的构造方式）：

```python
import struct
# FIFO 文件：偏移16处 magic=0xAE86(flag=0) → (raw_magic & 0xFFFF)==0xAE86
fifo = struct.pack("IIIIHHIQ6I", 64, 5, 1, 0, 0xAE86, 0, 0, 0, 0,0,0,0,0,0)
# workspace 文件：偏移16处 magic_num=0x5AA5BCCD
ws  = struct.pack("iiiiiiQ", 1024*1024, 0, 1, 0, 0x5AA5BCCD, 0, 0)
# 未知 magic
unk = struct.pack("IIIIHHIQ6I", 64, 1, 1, 0, 0xFFFF, 0, 0,0,0,0,0,0,0,0)

for name, data in [("fifo", fifo), ("ws", ws), ("unk", unk)]:
    raw = struct.unpack("I", data[16:20])[0]
    if raw is not None and (raw & 0xFFFF) == 0xAE86:
        print(name, "-> FifoDumpBinFile")
    elif raw == 0x5AA5BCCD:
        print(name, "-> DumpBinFile")
    else:
        print(name, "-> unknown magic", hex(raw))
```

**需要观察的现象**：三段字节流只在偏移 16 处不同，分发结果随之不同；FIFO 的 `raw_magic` 因 `flag=0` 恰为 `0x0000AE86`，workspace 的 `raw_magic` 恰为 `0x5AA5BCCD`。

**预期结果**：输出 `fifo -> FifoDumpBinFile`、`ws -> DumpBinFile`、`unk -> unknown magic 0xffff`。这与 `test_parse_fifo_flow` / `test_parse_legacy_flow` / `test_parse_unknown_magic` 三组测试的断言一致。完整端到端验证可直接运行：

```bash
cd <仓库根目录>
python3 -m pytest tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py -k "parse_fifo_flow or parse_legacy_flow or parse_unknown_magic" -v
```

**预期结果**：三个用例通过，且 `test_parse_unknown_magic` 验证了 unknown magic 不会让进程崩溃（`parse_dump_bin` 捕获异常返回 255，并在输出目录生成 `PARSER_*` 目录）。完整跑通需已安装 pytest 与 CANN 环境（部分用例 mock 了 `get_install_path`，无 CANN 也能跑分发类用例）；若无 pytest，可 `待本地验证`。

#### 4.3.5 小练习与答案

**练习 1**：若一个 FIFO 文件的 `flag` 字段（偏移 18–19）非 0，比如 `flag=1`（aiv），`raw_magic` 的值是多少？会被误判成 workspace 吗？

**参考答案**：`raw_magic = 0xAE86 | (1 << 16) = 0x0001AE86`。由于分发**先**判 `(raw_magic & 0xFFFF) == 0xAE86`（成立），会正确走 FIFO 分支，不会误判。这正是"先 FIFO 后 workspace"判定顺序的意义——测试 [`test_parse_fifo_flow_nonstandard_filename_core_type_from_flag`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L1515-L1527) 即用 `flag=1` 验证了 `core_type` 被映射为 `aiv`。

**练习 2**：分发函数为什么读 56 字节（`FifoBlockInfo.get_size()`）却只用其中 4 字节做 magic 判断？

**参考答案**：读 56 字节是为了顺带取出 `core_id`（偏移 4）和 `flag`（偏移 18）这两个 FIFO 后续要用的字段，一次 I/O 完成；magic 只需偏移 16–19 这 4 字节。workspace 分支则只关心偏移 16–19 的 magic_num，其余字段由各自的 `parse` 在打开文件后再完整解析。

---

## 5. 综合实践

**任务**：不依赖真实 NPU，纯用 Python 构造一个"含一条张量记录的 FIFO dump 文件"，喂给 `parse_dump_bin` 解析，确认产物符合预期。本任务把本讲三个最小模块（TLV、信封、分发）串起来。

**步骤**：

1. **拼信封**：按 4.3.4 的方式构造一个 `FifoBlockInfo` 头（`magic=0xAE86`、`core_id=3`、`flag=1` 表示 aiv）。
2. **拼一条 TLV**：tag 用 `DumpType.TENSOR_TYPE=2`；Value = `DumpMessageHeader`（`struct.pack("iiiiii", addr, data_type=3, desc=5, 0,0,0)`，`data_type=3` 表示 int32）+ 两个 int32 数据 `struct.pack("ii", 10, 20)`。TLV = `struct.pack("II", 2, len(value)) + value`。
3. **写盘**：`header + tlv` 写入 `asc_kernel_data_aiv_3.bin`（注意文件名要符合 `is_asc_kernel_data` 规则，否则会用 flag 回退，也能工作）。
4. **调用解析**：`from show_kernel_debug_data import show_kernel_debug_data; show_kernel_debug_data("./asc_kernel_data_aiv_3.bin", "./out")`，或命令行 `show_kernel_debug_data ./asc_kernel_data_aiv_3.bin ./out`。

**参考构造脚本**（示例代码，组装逻辑取自测试 [`_merged_FifoDumpCoreContentWrite__build_fifo_tensor_tlv`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L1253-L1279)）：

```python
import struct
# 1. 信封
header = struct.pack("IIIIHHIQ6I", 256, 3, 1, 0, 0xAE86, 1, 0, 0, 0,0,0,0,0,0)
# 2. 一条 int32 张量 TLV (desc=5, 值 10/20)
tensor_bytes = struct.pack("ii", 10, 20)
dmh = struct.pack("iiiiii", 0, 3, 5, 0, 0, 0)   # data_type=3(int32), desc=5
value = dmh + tensor_bytes
tlv = struct.pack("II", 2, len(value)) + value   # tag=2 (TENSOR_TYPE)
# 3. 写盘
open("asc_kernel_data_aiv_3.bin","wb").write(header + tlv)
```

**需要观察的现象**：

- 分发命中 FIFO（`raw_magic & 0xFFFF == 0xAE86`），`core_type` 来自文件名 `aiv`、`core_id` 来自文件名 `3`。
- 解析成功（返回 0），在 `./out/<PARSER_时间戳>/dump_data/3/` 下生成 `asc_kernel_data_aiv_3_index_5_loop_0.bin` 与 `.txt`。
- `.txt` 内含还原后的张量值 `10,20`；`dump_data/index_dtype.json` 记录 `{"5": "int32"}`。

**预期结果**：与测试 [`test_write_result_with_tensors`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L1301-L1314) 与 [`test_write_index_dtype`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L1329-L1344) 的断言一致。若手头没有安装好的 `show_kernel_debug_data` 包，可先 `pip install -e utils/show_kernel_debug_data`，或直接对照上述测试理解行为；端到端实际目录名带时间戳，`待本地验证`。

## 6. 本讲小结

- dump bin 的最小记录单元是 **TLV**：8 字节头（`"II"` = tag + length，均为 uint32）+ length 字节值。`SIMT_PRINTF_TYPE=0xF0E00F0E` 这类大 tag 值决定了头必须用 4 字节。
- 文件有两种形态：**workspace（legacy）** 多核拼接、每核一个 `BlockInfo`（32B，magic `0x5AA5BCCD`，块大小默认 1MiB）；**FIFO** 单核一份、用 `FifoBlockInfo`（56B，magic `0xAE86`）。两者是硬件 dump 通道演进的结果。
- `BlockInfo` 与 `FifoBlockInfo` 字段不同，但**魔数都写在文件偏移 16**，于是 `_read_block_magic_core_id_and_flag` 读偏移 16–19 这 4 字节即可同时探测。
- 顶层 [`parse_dump_bin`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L1201-L1253) 按 magic 分发：低 16 位 `0xAE86` → `FifoDumpBinFile`；整体 `0x5AA5BCCD` → `DumpBinFile`；否则报 `unknown block magic`。判定**先 FIFO 后 workspace**，避免 flag 非 0 时误判。
- 解析是**两级分发**：外层 magic 决定"信封怎么拆"，内层 TLV tag（`add_tlv_data`）决定"信纸怎么读"。
- FIFO 路径对每条 TLV 做了 `length > file_size` 与 `length > remain_size` 双重溢出校验，workspace 路径则靠 `core_dump_size` 上界兜底。
- `DumpTensor` 的 Value 内部还套了一层 24 字节的 `DumpMessageHeader`（含 data_type、desc），这是通往具体数据还原（printf/tensor/timestamp）的入口，由下一讲 u7-l3 展开。

## 7. 下一步学习建议

- **下一讲 u7-l3（printf / tensor / timestamp 解析实现）** 将深入 TLV 的 Value 内部：`PrintStruct._read_arg` 如何按 `%d/%f/%s` 还原参数、`DumpTensor._parse_dump_data` 如何用 `dtype_to_fmt` 表把字节还原成数值、`TimeStampInfo` 如何落成 CSV。建议本讲先把"TLV 切分"吃透，再去攻"Value 解读"。
- 若想验证理解，可通读 [`test_show_kernel_debug_data.py`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py) 中所有 `build_*` 辅助函数——它们用 `struct.pack` 还原了几乎每一种二进制结构，是最好的"活文档"。
- 横向对比：u6 讲过 Kernel ELF 的 `.ascend.meta` 也用 TLV（2 字节头）。可对比两种 TLV 头宽度、Length 单位与未知 Type 跳过策略的异同，加深对"自描述二进制格式"这一通用模式的理解。
- 对 bf16 数据还原感兴趣的话，可读 [`data_converter.py` 的 `decode_bfloat16`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/data_converter.py#L16-L36)，并结合 u3-l4 的 bf16 仿真原理，理解"同样的比特布局在 kernel 侧与解析侧必须一致"。
