# printf / tensor / timestamp 解析实现

## 1. 本讲目标

本讲是「show_kernel_debug_data 解析工具」单元的最后一讲，承接 u7-l2 讲清楚的 TLV 信封结构，往下走一层：当外层 magic 把信封拆开、内层 TLV 的 tag 把信纸分好类之后，**具体每一条信纸（DumpTensor / PrintStruct / TimeStampInfo）是怎么被还原成人能读懂的文本的**。

学完后你应当能够：

- 说清楚 `PrintStruct` 如何从一段二进制里把「格式化字符串 + 一组参数」还原成最终打印文本，尤其是整型、浮点、字符串三类参数各自的读取方式。
- 说清楚 `DumpTensor` 如何借助 `DumpMessageHeader` 拿到数据类型，再按类型把裸字节还原成数值列表，以及 bfloat16 这类「无原生格式」类型的特殊处理。
- 说清楚 `TimeStampInfo` 的字段含义，以及它最终如何被落盘成带「Cycle 间隔」的 CSV。
- 能够独立构造一个最小的 printf TLV，调用 `PrintStruct().parse_from(...)` 验证自己的理解。

## 2. 前置知识

本讲默认你已经掌握 u7-l1（Dump 的配置与生成）与 u7-l2（dump bin 的 TLV 二进制布局）。在此基础上补充三个 Python 侧的小知识点：

- **`struct` 模块**：Python 标准库里用来处理「C 风格定长二进制」的工具。格式串 `"IIQQ"` 表示「2 个 uint32 + 2 个 uint64」，`struct.unpack(fmt, buffer)` 按格式把字节解成一 tuple，`struct.iter_unpack(fmt, buffer)` 则按固定步长反复解包，非常适合「一串同类型元素」的批量还原。
- **dataclass**：`@dataclass` 装饰器能自动生成 `__init__` 等方法，dump_parser.py 里几乎所有消息类（`TLV`、`DumpMessageHeader`、`DumpTensor`、`PrintStruct`、`TimeStampInfo`）都是 dataclass，字段就是二进制里要填的槽位。
- **C 的 `printf` 可变参数**：kernel 侧的 `AscendC::printf("a=%d b=%f\n", 123, 3.14)` 和 C 的 printf 一样是「格式串 + 可变参数」。运行时把每个参数都塞进一个 8 字节定长槽，再把格式串跟在后面——这是本讲 `PrintStruct` 解析的物理基础。

一句话回顾 u7-l2 的结论：解析是**两级分发**——外层 magic 决定信封怎么拆（workspace vs FIFO），内层 TLV 的 tag 决定信纸怎么读。本讲聚焦的就是「内层 tag 决定信纸怎么读」这一步，入口是 `DumpCoreContent.add_tlv_data`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [dump_parser.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py) | 本讲的核心。定义了 `PrintStruct`、`DumpTensor`、`TimeStampInfo` 等消息类及其 `parse_from`，以及总分发器 `add_tlv_data` 与各类落盘方法。 |
| [dump_logger.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_logger.py) | 日志工具。提供全局 `DUMP_PARSER_LOG`，被 dump_parser.py 用来打印 debug/info/warning/error，是解析过程的「观察窗口」。 |
| [data_converter.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/data_converter.py) | 提供 `decode_bfloat16`，被 `DumpTensor` 用来把 bf16 的 16 位比特手动还原成 float。 |
| [examples/01_show_kernel_debug_data/add.asc](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/add.asc) | 生成调试数据的算子样例，本讲用它对照「源码里写了什么 → 解析后打印什么」。 |
| [tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py) | 单元测试。里面用 `struct.pack` 手工构造了各种 TLV，是理解二进制布局最权威的「参考实现」。 |

## 4. 核心概念与源码讲解

在进入三个最小模块之前，先看一眼把所有信纸串起来的总分发器。`DumpCoreContent.add_tlv_data` 根据 TLV 的 tag 把二进制分发给不同的消息类：

[dump_parser.py:L745-L783](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L745-L783) —— 按 `tlv.tag` 分发：`TENSOR_TYPE` 给 `DumpTensor`、`SCALAR_TYPE`/`ASSERT_TYPE`/`SIMT_PRINTF_TYPE`/`SIMT_ASSERT_TYPE` 给 `PrintStruct`、`TIME_STAMP` 给 `TimeStampInfo`、`SHAPE_TYPE` 给 `ShapeInfo`、`META_TYPE` 给 `MetaInfo`。

tag 的取值定义在 `DumpType` 枚举里：

[dump_parser.py:L904-L913](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L904-L913) —— 注意 `SIMT_PRINTF_TYPE = 0xF0E00F0E`，这就是 u7-l2 提到的「SIMT printf 的 tag 是个大数，所以 TLV 头必须用 4 字节」的来源。

还有一个隐含规则值得先记住：`SHAPE_TYPE` 会把形状暂存到 `self.shape`，**紧接着的下一个** `TENSOR_TYPE` 会消费它（`dump_tensor.dump_shape = self.shape.copy()` 后立刻 `clear()`）。也就是说形状信息是「前置」在 tensor 之前的，顺序不能乱。本讲后续会用到这一点。

下面按三个最小模块展开。

### 4.1 PrintStruct 格式串与参数解析

#### 4.1.1 概念说明

kernel 侧一句 `AscendC::printf("fmt string int: %d\n", 0x123)` 落盘后，对应一条 tag 为 `SCALAR_TYPE`（或 SIMT 场景下的 `SIMT_PRINTF_TYPE`）的 TLV。这条 TLV 的 Value 不是简单的文本，而是按固定规则打包的「格式串 + 参数数组」。`PrintStruct` 的工作就是逆向这个过程：把格式串读出来、把参数一个个按类型读出来、最后用 Python 的 `%` 格式化拼回成最终文本。

为什么运行时不直接落盘文本？因为 NPU/kernel 侧追求极简、定长、零解析开销：所有参数统一塞进 8 字节定长槽，格式串单独放在末尾，这样写入端只管「搬 8 字节、搬 8 字节、再拷一段字符串」，复杂度全留给离线解析端。

#### 4.1.2 核心流程

一条 printf TLV 的 Value 布局如下（`PrintStruct` 的 legacy 形态）：

```
偏移       内容                         说明
[0   : 8  ] fmt_offset (uint64)          格式串的绝对偏移（= 8 + 参数总长）
[8   : fmt_offset] args 区域              每个参数占 8 字节定长槽
[fmt_offset: ...] 格式串 + '\0'           C 风格以 \0 结尾
```

解析流程（伪代码）：

```
1. _read_fmt(buffer):
     在偏移 0 读 8 字节得到 fmt_offset
     在 fmt_offset 处读取以 \0 结尾的格式串
     参数区 = [8, fmt_offset)
2. _all_fmt_placehold(fmt):
     用正则 %[a-zA-Z]{1,2} 找出所有占位符
     过滤掉「3 字符且不含 %l」的误匹配（如正文里的 "50%off"）
3. 对第 i 个占位符:
     按 args_start + i*8 定位它的 8 字节槽
     依据占位符类型(%d/%f/%s/...)用对应方法读值
4. 把 "%p" 替换成 "0x%x"（Python 不支持 %p）
5. content = fmt % tuple(args)
```

一个关键直觉：**格式串既是「模板」又是「类型说明」**。`_read_arg` 完全靠格式串里的占位符决定每个 8 字节槽该怎么解——`%d` 当有符号 8 字节整数、`%f` 当浮点、`%s` 当「指向字符串的相对指针」。

#### 4.1.3 源码精读

入口 `PrintStruct.parse_from`：

[dump_parser.py:L403-L411](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L403-L411) —— 依次读格式串、读参数，再把 `%p` 改写成 `0x%x`，最后用 `self.fmt % tuple(self.args)` 生成文本。最后这一步是「借用 Python 自带 printf 语义」来做格式化，所以前面必须把 `%p` 这种 Python 不认的先换掉。

读格式串 `_read_fmt` 与字符串读取 `_read_arg_str`：

[dump_parser.py:L413-L415](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L413-L415) —— `_read_fmt` 在偏移 0 调用 `_read_arg_str`，返回 `(args_start=8, fmt_offset)`。

[dump_parser.py:L489-L493](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L489-L493) —— `_read_arg_str` 先读 8 字节当「相对偏移」，再加回自身偏移得到字符串的绝对位置。注意它返回的是 `(字符串, 相对偏移)` 二元组，这个相对偏移正是 `%s` 参数槽里存的东西。

找出占位符 `_all_fmt_placehold`：

[dump_parser.py:L417-L426](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L417-L426) —— 正则 `%[a-zA-Z]{1,2}` 只匹配「% 加 1~2 个字母」，真实占位符要么是 `%X`（2 字符）要么是 `%lX`（3 字符且带 `l`）。于是「3 字符且不含 `%l`」的就是正文里的误匹配（例如 `"100%off"` 里的 `%of`），直接丢弃。

按占位符类型分发 `_read_arg`：

[dump_parser.py:L441-L460](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L441-L460) —— 这是本模块的「调度中心」。各占位符与读取方法的对应关系汇总如下：

| 占位符 | 读取方法 | struct 格式 | 字节数 | 含义 |
|--------|----------|-------------|--------|------|
| `%d` `%i` `%ld` | `_read_arg_long` | `q`（有符号 8B） | 8 | 整数 |
| `%u` | `_read_arg_unsigned_long` | `Q`（无符号 8B） | 8 | 无符号整数 |
| `%x` `%X` | `_read_arg_hex` | `Q`（无符号 8B） | 8 | 十六进制 |
| `%lf` `%LF` | `_read_arg_double` | `d`（double 8B） | 8 | 双精度浮点 |
| `%f` `%F` | `_read_arg_float` | `f` 或 `d` | 4 或 8 | 单/双精度（自动判别） |
| `%p` | `_read_arg_point` | `P`（原生指针） | 8 | 指针 |
| `%s` | `_read_arg_str` | `Q`（相对偏移） | 8 + 变长 | 字符串 |

整型读取 `_read_arg_long` / `_read_arg_unsigned_long` / `_read_arg_hex`：

[dump_parser.py:L466-L468](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L466-L468) —— 整型最直白：8 字节槽按 `q`（有符号）解。`%u`/`%x` 则按 `Q`（无符号）解，区别只在最终格式化时 Python 把它当有符号还是无符号、十进制还是十六进制。

浮点读取 `_read_arg_float`（本模块最巧妙的一处）：

[dump_parser.py:L470-L479](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L470-L479) —— `%f` 在 C 里可能是 `float` 也可能（经过默认参数提升后）是 `double`，二者共用 `%f` 占位符但字节宽度不同。代码用一个启发式来判别：检查这 8 字节槽的**高 4 字节** `[offset+4 : offset+8]` 是否全为 0。若全 0，说明只有低 4 字节有效，按 `f`（单精度）解；若有非零字节，说明是 8 字节 double，按 `d` 解。背后原理：正常的 32 位 float 放进 8 字节槽时高位必为 0，而 double 的指数/符号位通常让高位非零。这是一个实用但不完美的启发式（极端 double 值可能误判，待本地验证边界）。

字符串读取 `_read_arg_str` + `_read_string`：

[dump_parser.py:L499-L512](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L499-L512) —— 字符串是变长的，没法塞进 8 字节槽，所以 8 字节槽里存的是「相对偏移」，指向真正字符串的位置；`_read_string` 从该位置逐字节读，直到遇到 `\x00` 结束。这就是为什么 printf 的二进制布局是「定长参数区 + 变长字符串拖在后面」。

把上面的流程套到样例 `AscendC::printf("fmt string int: %d\n", 0x123)` 上：

- 格式串 = `"fmt string int: %d\n"`，占位符 = `["%d"]`
- 参数 0 用 `_read_arg_long` 读出 `0x123 = 291`
- `content = "fmt string int: %d\n" % (291,) = "fmt string int: 291\n"`

这与样例 README 给出的运行输出 `fmt string int: 291` 完全一致（见 [examples/01_show_kernel_debug_data/README.md:L109-L122](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/README.md#L109-L122)）。同理 `"fmt string float: %f\n"` 配 `3.14` 会得到 `fmt string float: 3.140000`。

最后提一句 FIFO 形态的差别。`FifoPrintStruct` 在标准 payload 前多 8 字节头（`blockIdx` + `resv`），解析时先跳过这 8 字节再复用父类的 `_read_fmt`/`_read_args`：

[dump_parser.py:L515-L534](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L515-L534) —— FIFO 的 printf 多带了 `block_idx`，用来标识是哪个核打印的。

而 `FifoSimtPrintStruct`（SIMT/warp 场景）头部更大（`3I3I4IQ` = 40 字节），携带 `block_idx[3]` 与 `thread_idx[3]`，因为 SIMT 下要区分到「哪个线程」打印的：

[dump_parser.py:L537-L562](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L537-L562) —— `thread_idx` 会被拼成 `"4_5_6"` 这样的 key，落盘时按线程拆分到不同文件。

#### 4.1.4 代码实践

**实践目标**：手工构造一条 printf TLV，调用 `PrintStruct().parse_from(...)`，亲眼看整型/浮点/字符串参数是怎么被还原的。

**操作步骤**：

1. 进入仓库根目录，把工具包加入 `PYTHONPATH`：

   ```bash
   cd /path/to/asc-tools
   export PYTHONPATH="$PWD/utils/show_kernel_debug_data:$PYTHONPATH"
   ```

2. 新建一个临时脚本 `tmp_trace_printf.py`（**示例代码**，非项目原有文件，可放在任意临时目录）：

   ```python
   import struct
   from show_kernel_debug_data.dump_parser import PrintStruct, DumpType, TLV

   def make_print_tlv(fmt, args_bytes):
       """模仿测试用例手工拼一条 printf TLV。"""
       fmt_bytes = fmt.encode("utf-8") + b"\x00"
       fmt_offset = 8 + len(args_bytes)            # 格式串绝对偏移 = 8 + 参数区长度
       tlv_value = struct.pack("Q", fmt_offset) + args_bytes + fmt_bytes
       return TLV(tag=DumpType.SCALAR_TYPE.value, length=len(tlv_value), value=tlv_value)

   # 1) 整型：%d，参数按 q（8 字节有符号）打包
   tlv_int = make_print_tlv("int=%d\n", struct.pack("q", 0x123))
   ps = PrintStruct(); ps.parse_from(tlv_int)
   print("[int ]", repr(ps.content), "args=", ps.args)

   # 2) 浮点：%lf，参数按 d（8 字节 double）打包
   tlv_f = make_print_tlv("float=%f\n", struct.pack("d", 3.14))
   ps = PrintStruct(); ps.parse_from(tlv_f)
   print("[float]", repr(ps.content), "args=", ps.args)

   # 3) 字符串：%s，参数槽存相对偏移，字符串本体拖在后面
   str_body = "hello".encode("utf-8") + b"\x00"
   args_bytes = struct.pack("Q", 8) + str_body     # 相对偏移 8 → 指向紧跟其后的字符串
   tlv_s = make_print_tlv("str=%s\n", args_bytes)
   ps = PrintStruct(); ps.parse_from(tlv_s)
   print("[str  ]", repr(ps.content), "args=", ps.args)
   ```

3. 运行 `python3 tmp_trace_printf.py`。

**需要观察的现象**：

- 三行 `[int ]` / `[float]` / `[str  ]` 的 `content` 分别应当是 `'int=291\n'`、`'float=3.140000\n'`、`'str=hello\n'`。
- `args` 列表分别是 `[291]`、`[3.14...]`、`['hello']`，对应 `_read_arg_long`、`_read_arg_double`（经 `_read_arg_float` 的启发式判别为 double）、`_read_arg_str` 三条路径。

**预期结果**：与上面一致。其中整型 `0x123` 解出 `291`，和样例 README 的 `fmt string int: 291` 同源；浮点 `%f` 走 `_read_arg_float` 时因高 4 字节非零被判为 double。

**说明**：若 `%f` 参数你改成 `struct.pack("f", 3.14) + b"\x00"*4`（单精度 + 高位补零），`_read_arg_float` 会走 `f` 分支，可对照观察启发式的判别行为。本实践的断言与项目单元测试 `test_format_ld` / `test_format_lf` / `test_format_s`（见测试文件 L1012-L1063）一致，运行结果可本地验证。

#### 4.1.5 小练习与答案

**练习 1**：格式串 `"ratio=%d%%\n"`（含两个百分号，第一个是 `%d` 占位符，第二个 `%%` 是字面量）会被 `_all_fmt_placehold` 解析出几个占位符？为什么？

> **答案**：1 个。正则 `%[a-zA-Z]{1,2}` 要求 `%` 后**必须跟字母**，而 `%%` 第二个是 `%` 不是字母，不匹配。所以只识别出 `%d`，`%%` 会被原样留给 Python 的 `%` 格式化（Python 中 `%%` 表示字面 `%`）。

**练习 2**：为什么 `_read_arg_float` 要检查高 4 字节，而 `_read_arg_double` 不用检查、直接按 8 字节解？

> **答案**：`%f` 的实际类型有歧义（float 或 double），需要启发式判别；而 `%lf` 在格式串里已经**显式声明**是 double，没有歧义，所以直接 `struct.unpack("d", ...)` 读满 8 字节即可。

---

### 4.2 DumpTensor 数据片段还原

#### 4.2.1 概念说明

`AscendC::DumpTensor(xLocal[64], 0, 16)` 的语义是「从 `xLocal` 的第 64 个元素开始，dump 16 个元素，编号（desc）为 0」。它落盘后是一条 tag 为 `TENSOR_TYPE` 的 TLV。和 printf 不同，tensor 没有「格式串」，但多了一个**定长头部** `DumpMessageHeader` 来记录元信息（数据类型、编号等），头部之后就是一段**同类型元素的裸字节流**。

`DumpTensor` 的任务：先读头部拿到 `data_type`，再按类型把裸字节流还原成一个数值列表 `dump_value`。

#### 4.2.2 核心流程

一条 tensor TLV 的 Value 布局（legacy 形态）：

```
[0       : 24  ] DumpMessageHeader (iiiiii = 6×int32)
                 addr, data_type, desc, buffer_id, position, reserved
[24      : ... ] 裸数据字节流，元素类型由 data_type 决定
```

解析流程：

```
1. 从 Value 前 24 字节解出 DumpMessageHeader（关键: data_type 与 desc）
2. dump_data = Value[24:]                       剩余字节即裸数据
3. 按 data_type 查 dtype_to_fmt 得到 struct 格式字符
4. struct.iter_unpack(fmt, dump_data) 逐元素还原成 dump_value
   - 若是 bfloat16(27)：先按 uint16("H")解，再逐个 decode_bfloat16
```

#### 4.2.3 源码精读

头部 `DumpMessageHeader`：

[dump_parser.py:L100-L127](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L100-L127) —— 格式 `"iiiiii"`，6 个 int32 共 24 字节。其中 `data_type` 决定数据怎么解、`desc` 就是 `DumpTensor(tensor, desc, count)` 的第二个参数（样例里 0/1/2 分别对应 xLocal/yLocal/zLocal，见 README L140）。

`DumpTensor.parse_from`：

[dump_parser.py:L315-L324](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L315-L324) —— 切出前 24 字节头部、剩余作为 `dump_data`，再调用 `_parse_dump_data`。

数据类型到 struct 格式的映射表：

[dump_parser.py:L271-L284](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L271-L284) —— 汇总如下：

| data_type | 含义 | struct 格式 |
|-----------|------|-------------|
| 0 | DT_FLOAT | `f`（float32） |
| 1 | DT_FLOAT16 | `e`（half） |
| 2 | DT_INT8 | `b` |
| 3 | DT_INT32 | `i` |
| 4 | DT_UINT8 | `B` |
| 6 | DT_INT16 | `h` |
| 7 | DT_UINT16 | `H` |
| 8 | DT_UINT32 | `I` |
| 9 | DT_INT64 | `q` |
| 10 | DT_UINT64 | `Q` |
| 27 | DT_BF16 | `H`（按 uint16 解后特殊处理） |
| 33 | DT_MAX | ``（不支持） |

真正干活的 `_parse_dump_data`：

[dump_parser.py:L340-L356](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L340-L356) —— 用 `struct.iter_unpack` 按固定步长反复解包。关键分支：bfloat16（27）落在 `special_data_type` 字典里，会先按 `H`（uint16）解出原始 16 位比特，再逐个用 `decode_bfloat16` 转成 float；其余类型直接取 `iter_unpack` 的结果。`data_type` 不在表里时（如 33）记一条 debug 日志后返回，`dump_value` 为空。

bfloat16 为什么需要「特殊处理」？因为 Python 的 `struct` **没有 bf16 原生格式**。bf16 与 float32 共享同一个指数位宽（8 位），只是尾数只有 7 位，所以代码按 uint16 读出比特后，手动拆 sign/exponent/mantissa 还原成 float，实现在 [data_converter.py:L16-L36](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/data_converter.py#L16-L36)。

落盘成文本 `_write_dump_tensor_value`：

[dump_parser.py:L678-L727](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L678-L727) —— 若 tensor 携带 `dump_shape`（由前置的 `SHAPE_TYPE` TLV 注入），按多维括号格式排版；否则按「每行 8 个、逗号分隔」平铺。还有一处容错：当 shape 需要的元素数多于实际 dump 出来的，缺位用 `"-"` 填充；反之多余值被忽略，两种情况都会打 warning。

FIFO 形态 `FifoDumpTensor` 用了更大的头部 `IIIIHHI8III`（68 字节），把 `block_idx`、`dim`、`shape[8]`、`dump_size` 都内联进了头部：

[dump_parser.py:L359-L392](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L359-L392) —— 注意它按 `dump_size`（而非「读到尾部」）切数据，并做了 `dump_size out of range` 的边界校验，防止越界读。

套到样例 `DumpTensor(xLocal[64], 0, 16)`：`desc=0`、`data_type=0`（float），所以解析后进 `index_0` 目录，`dump_value` 是 16 个 `1.2`（因为输入 x 全为 1.2f），`index_dtype.json` 里会记 `{0: "float32"}`（dtype_to_data_type[0]，见 [dump_parser.py:L286-L299](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L286-L299)）。

#### 4.2.4 代码实践

**实践目标**：构造一条 int32 的 tensor TLV，观察 `DumpTensor` 如何把头部与数据分开、并按类型还原。

**操作步骤**：

1. 同样先把工具包加入 `PYTHONPATH`（同 4.1.4 第 1 步）。
2. 新建临时脚本 `tmp_trace_tensor.py`（**示例代码**）：

   ```python
   import struct
   from show_kernel_debug_data.dump_parser import DumpTensor, DumpType, TLV

   # 头部: addr=0, data_type=3(int32), desc=5, 其余 0
   header = struct.pack("iiiiii", 0, 3, 5, 0, 0, 0)
   data   = struct.pack("ii", 10, 20)             # 两个 int32
   tlv = TLV(tag=DumpType.TENSOR_TYPE.value,
             length=len(header + data), value=header + data)

   t = DumpTensor(); t.parse_from(tlv)
   print("data_type =", t.dump_header.data_type)
   print("desc      =", t.dump_header.desc)
   print("dump_value=", t.dump_value)
   ```

3. 运行 `python3 tmp_trace_tensor.py`。

**需要观察的现象**：`data_type=3`、`desc=5`、`dump_value=[10, 20]`。

**预期结果**：与上述一致——头部 24 字节被解出，剩余 8 字节按 `i`（int32）解出两个元素 `[10, 20]`。该断言与单元测试 `test_parse_from_dump_tensor`（测试文件 L349-L358）完全同源，可本地验证。

**进阶观察（源码阅读型）**：把 `data_type` 改成 `27`（bf16），数据改成 `struct.pack("HH", 16256, 16384)`，对照 [data_converter.py:L16-L36](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/data_converter.py#L16-L36) 与 `decode_bfloat16(0x3F80)==1` 的测试断言（测试文件 L199），理解「uint16 → 手动还原 float」的路径。结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果一条 tensor TLV 的 `data_type = 33`（DT_MAX），`dump_value` 会是什么？为什么？

> **答案**：空列表 `[]`。`dtype_to_fmt[33]` 是空字符串，`_parse_dump_data` 在 `if not fmt` 分支记一条 debug 日志后直接 return，不往 `dump_value` 里塞任何东西。

**练习 2**：legacy `DumpTensor` 靠什么确定数据的长度？`FifoDumpTensor` 又是靠什么？

> **答案**：legacy `DumpTensor` 靠「TLV 的 Value 去掉前 24 字节头部，剩下的全是数据」，即隐式由 TLV length 决定；`FifoDumpTensor` 则在头部里显式带了一个 `dump_size` 字段，按它精确切片，并校验 `dump_size` 不能超出剩余字节。

---

### 4.3 TimeStamp 解析与落盘

#### 4.3.1 概念说明

`AscendC::PrintTimeStamp(id)` 用来在 kernel 执行过程中「打点」，记录某一刻的系统 cycle 计数与 PC 指针，用于性能分析。它落盘后是一条 tag 为 `TIME_STAMP` 的 TLV。`TimeStampInfo` 的任务很简单：把定长字段解出来；真正有信息量的是落盘时把 `desc_id` 翻译成人能看懂的「打点标识」，并算出相邻两点之间的 cycle 间隔。

#### 4.3.2 核心流程

```
1. 按 "IIQQ" 解出 desc_id, rsv, sys_cycle, pc_ptr
2. 落盘时:
   - 用 get_enum_member_name(TimeStampId, desc_id) 把数字翻译成名字
   - 计算 Cycle间隔 = 当前 sys_cycle - 上一条 sys_cycle
   - 写成 CSV: 打点标识, Cycle, Cycle间隔, PC指针
```

#### 4.3.3 源码精读

字段定义 `TimeStampInfo`：

[dump_parser.py:L206-L228](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L206-L228) —— 格式 `"IIQQ"`：`desc_id`（uint32）、`rsv`（uint32）、`sys_cycle`（uint64）、`pc_ptr`（uint64），共 24 字节。`sys_cycle` 是硬件 cycle 计数，`pc_ptr` 是打点处的程序计数器。

打点标识枚举 `TimeStampId`：

[dump_parser.py:L31-L54](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L31-L54) —— 预定义了一组「语义化打点名」，例如 `TIME_STAMP_TPIPE = 0x030`、`TIME_STAMP_BUFFER`、`TIME_STAMP_MATMUL_SERVER` 等，覆盖 TPipe、Buffer、Matmul、TilingData 几大类框架内置打点。`get_enum_member_name`（L57-L61）按值查表，查不到就把原始数字原样输出（测试 `test_get_non_existing_member_value` 验证：2457 查不到就返回 2457）。

FIFO 形态 `FifoTimeStampInfo`：

[dump_parser.py:L234-L265](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L234-L265) —— 格式 `"IHHQQQII"`，比 legacy 多了 `block_idx` 与 `entry` 字段，用于标识「哪个核、哪个入口」的时间戳。

落盘 `_write_time_stamp`：

[dump_parser.py:L860-L881](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py#L860-L881) —— 写出 `time_stamp_core_<id>.csv`，表头为 `["打点标识", "Cycle", "Cycle间隔", "PC指针"]`。核心是「Cycle 间隔」：用 `last_cycle` 记住上一条的 cycle，当前减上一条得到间隔，再更新 `last_cycle`。第一条的间隔就是它自身的 cycle（因为 `last_cycle` 初值为 0）。`desc_id` 经 `get_enum_member_name` 翻译，例如 48 显示成 `TIME_STAMP_TPIPE`。

#### 4.3.4 代码实践

**实践目标**：构造两条连续的 timestamp TLV，加入 `DumpCoreContent`，落盘后检查 CSV 里「Cycle 间隔」的计算。

**操作步骤**：

1. 把工具包加入 `PYTHONPATH`（同前）。
2. 新建临时脚本 `tmp_trace_ts.py`（**示例代码**）：

   ```python
   import struct, tempfile, os
   from show_kernel_debug_data.dump_parser import DumpCoreContent, DumpType, TLV

   def ts_tlv(desc_id, sys_cycle, pc=0xABC):
       return TLV(tag=DumpType.TIME_STAMP.value,
                  length=24,
                  value=struct.pack("IIQQ", desc_id, 0, sys_cycle, pc))

   content = DumpCoreContent()
   content.add_tlv_data(ts_tlv(48, 1000))   # TIME_STAMP_TPIPE
   content.add_tlv_data(ts_tlv(49, 1250))   # TIME_STAMP_BUFFER(auto 后 = 0x031=49)

   out = tempfile.mkdtemp()
   content._write_time_stamp(out)
   csv_file = os.path.join(out, "time_stamp_core_unknown.csv")
   print(open(csv_file, encoding="utf-8-sig").read())
   ```

3. 运行 `python3 tmp_trace_ts.py`。

**需要观察的现象**：CSV 两行数据，`Cycle间隔` 列第一条为 1000、第二条为 250；`打点标识` 列显示 `TIME_STAMP_TPIPE` 等。

**预期结果**：与上述一致——`1250 - 1000 = 250` 验证了「相邻差值」逻辑，`desc_id=48` 被翻译成 `TIME_STAMP_TPIPE` 验证了枚举查找。该逻辑与单元测试 `test_write_time_stamp`（测试文件 L1218-L1229）同源，可本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果一条 timestamp 的 `desc_id` 在 `TimeStampId` 枚举里不存在，CSV 的「打点标识」列会显示什么？

> **答案**：显示原始数字本身。`get_enum_member_name` 遍历枚举找不到匹配的 value 时，直接 `return value`（即原始数字），不会报错。

**练习 2**：为什么第一条 timestamp 的「Cycle 间隔」往往等于它自己的 `sys_cycle`？

> **答案**：`_write_time_stamp` 里 `last_cycle` 初值为 0，第一条的间隔 = `sys_cycle - 0 = sys_cycle`。所以只有在「从第 2 条起」时，间隔才是真正意义上的「两点之差」。

---

## 5. 综合实践

把三个模块串起来：模拟一小段 add 算子运行产生的调试数据，一次性体验 printf、tensor、timestamp 三类消息的端到端解析。

**任务**：用 `struct` 手工拼出一个最小 workspace dump bin（用 legacy `BlockInfo` + 一串 TLV），包含「一条 SHAPE_TYPE + 一条 TENSOR_TYPE + 一条 SCALAR_TYPE(printf) + 一条 TIME_STAMP」，然后用 `DumpBinFile` 解析，检查产物。

**参考步骤**：

1. 构造 `BlockInfo`：`struct.pack("iiiiiiQ", 1024*1024, 0, 1, 0, 0x5AA5BCCD, 0, 0)`（魔数 `0x5AA5BCCD`，total_size 设 1MiB，u7-l2 已讲过这个魔数）。
2. 依次拼接 TLV（每条 TLV = `struct.pack("II", tag, length) + value`）：
   - `SHAPE_TYPE(3)`：value = `struct.pack("iiiiiiiiii", 1, 4, 0,0,0,0,0,0,0,0)`（一维，长度 4）。
   - `TENSOR_TYPE(2)`：value = 24 字节头（`data_type=3`、`desc=0`）+ `struct.pack("iiii", 1, 2, 3, 4)`。
   - `SCALAR_TYPE(1)`：用 4.1.4 的 `make_print_tlv("sum=%d\n", struct.pack("q", 10))`。
   - `TIME_STAMP(6)`：`struct.pack("IIQQ", 48, 0, 1000, 0xABC)`。
3. 把 `BlockInfo + 各 TLV` 写入 `dump.bin`（不足 1MiB 的部分用 `\x00` 填满，因为 total_size=1MiB）。
4. 调用：

   ```python
   from show_kernel_debug_data.dump_parser import DumpBinFile
   DumpBinFile("dump.bin").parse()  # 或直接 parse_dump_bin("dump.bin", "./out")
   ```

5. 检查 `./out` 下是否生成了 `dump_data/<core_id>/index_0/*.txt`（tensor，注意它应带上前置的 shape `[4]`）、`time_stamp_core_*.csv`，以及 stdout 是否打印了 `sum=10`。

**需要观察的现象**：

- tensor 的 `.txt` 因前置了 `SHAPE_TYPE`，会按 shape `[4]` 排版而非「每行 8 个平铺」——这验证了 4. 开头提到的「shape 前置注入」规则。
- printf 的 `sum=10` 出现在 `show_print` 打印的 `block.* begin/end` 之间。
- timestamp 出现在 CSV 中，`打点标识` 为 `TIME_STAMP_TPIPE`。

**预期结果**：上述三类产物都正确生成。本综合实践融合了 `add_tlv_data` 分发、`SHAPE_TYPE→TENSOR_TYPE` 前置注入、三类消息的 `parse_from` 与落盘，是检验本讲理解程度的标尺。各分支行为均有对应单元测试覆盖（如 `test_add_tlv_data_tensor/shape/timestamp`、`test_parse_dump_shape`），可对照本地验证。

## 6. 本讲小结

- **总分发器** `add_tlv_data` 按 TLV 的 tag 把二进制路由给 `DumpTensor` / `PrintStruct` / `TimeStampInfo` 等消息类；其中 `SHAPE_TYPE` 会把形状前置暂存，供下一条 `TENSOR_TYPE` 消费。
- **PrintStruct** 靠格式串里的占位符（`%d`/`%f`/`%s`/...）决定每个 8 字节参数槽的解包方式；`%f` 用「检查高 4 字节是否非零」的启发式区分 float 与 double；`%s` 的参数槽存的是「相对偏移」，指向拖在后面的变长字符串。
- **DumpTensor** 先读 24 字节 `DumpMessageHeader` 拿 `data_type`，再用 `struct.iter_unpack` 按类型批量还原；bfloat16 因无原生 struct 格式，按 uint16 解出比特后用 `decode_bfloat16` 手动还原。
- **TimeStampInfo** 字段为 `desc_id / sys_cycle / pc_ptr`，落盘成 CSV 时把 `desc_id` 翻译成 `TimeStampId` 枚举名，并计算相邻两点的 cycle 间隔。
- 三类消息都有 legacy 与 FIFO 两套形态，FIFO 形态多带 `block_idx`（及 SIMT 下的 `thread_idx`）以区分核/线程，解析时先跳过额外头部再复用同一套 `_read_*` 逻辑。
- dump_logger.py 提供的 `DUMP_PARSER_LOG` 贯穿整个解析过程，是观察「不支持的数据类型」「shape 不匹配」等异常情况的窗口，日志级别用 CANN 惯例（0=DEBUG … 3=ERROR）。

## 7. 下一步学习建议

本讲讲完了 show_kernel_debug_data 工具的「最内层」——单条消息如何还原。接下来建议：

- **横向打通三个 Python 工具**：本讲（调试 bin 解析）、u6（msobjdump 的 ELF 解析）、u5（npuchk 日志解析）都是「把机器向产物翻译成人读报告」的薄 Python 工具，可以对比它们在「二进制布局假设 + 外部命令配合」上的异同。
- **向上回看 cpudebug**：printf/DumpTensor/PrintTimeStamp 在 CPU 域是经 stub 注册（u3-l3）绑定到 `cceprint`/`AscendC` 实现的，可结合 u3-l3 理解「kernel 调用 → stub → 落盘 bin」的完整链路。
- **进入第 9 单元（构建与测试）**：本讲反复引用的 `tests/py_ut/testcase/show_kernel_debug_data/` 是 Python UT 的一部分，学完 u9-l3 可系统理解 `bash build.sh --python_utest` 如何驱动这些用例，以及如何为本工具新增测试。
