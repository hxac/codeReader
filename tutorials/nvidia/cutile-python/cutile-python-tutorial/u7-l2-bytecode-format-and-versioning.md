# 字节码格式与版本：writer、encodings、类型表

## 1. 本讲目标

上一讲（u7-l1）我们讲了「怎么把树形 Tile IR 压扁成线性字节码」——也就是 `generate_bytecode_for_kernel` 如何遍历 `Block`、用 `CodeBuilder.new_op` 逐条降级、如何处理嵌套 region。本讲接着往下钻一层：**这些被压扁的字节，到底以怎样的二进制格式落盘？怎么组织成文件？怎么随版本演进？**

读完本讲，你应当能够：

1. 说清一个 `.tileirbc` 文件的**整体布局**：magic 头之后是一串带 tag 的 section，最后以 `EndOfBytecode` 收尾；并能按 section 顺序说出 Func / Global / Constant / Debug / Type / String 各段的作用。
2. 解释 `BytecodeVersion`（`V_13_1/2/3`，外加 dev 的 `V_13_4`）如何用整数编码主/次/tag、如何作为 `IntEnum` 做**特性门控**，以及 `_get_max_supported_bytecode_version` 如何用「空字节码探针」探测后端 `tileiras` 支持的最高版本。
3. 读懂 `_write_table` 这套**通用表编码**（项数 + 偏移数组 + 数据区），并理解 String/Constant/Type 三张表都复用同一套机制。
4. 掌握 `TypeTable` 如何用「字节串作键、自增 id 作值」做**类型去重**，以及简单类型与复合类型（Tile/Pointer/Function…）的编码方式。
5. 看懂任何一个 `encode_*Op` 函数的**固定套路**（opcode → 结果类型 → flags → attributes → operands → `new_op`），并理解两种版本门控写法（「新特性只在 X+ 写入」与「旧版本下断言取默认值」）。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **ir2bytecode 的总体职责**（u7-l1）：`generate_bytecode_for_kernel` 按「函数/signature」粒度工作，开 `writer.function`、构造 `BytecodeContext`、递归 `generate_bytecode_for_block` 把每条 IR `Operation` 编码成线性字节码。本讲回答「`writer.function` 写出去的字节长什么样、`CodeBuilder.buf` 里的字节最终怎么打包成文件」。
- **`CodeBuilder.new_op` 分配结果 value id**（u7-l1）：每条 op 编码完，`new_op` 用单调递增的 `next_value_id` 认领结果；嵌套 region 用 `new_op_with_nested_blocks` + `NestedBlockBuilder.new_block` 切换临时缓冲。本讲里的 `encode_*Op` 就是「往 `code_builder.buf` 里写一堆字节，最后调 `new_op`」。
- **Tile IR 的类型对象**（u5-l6）：`ArrayTy`/`TileTy` 等是编译期 Python 对象；本讲讲的是它们如何被序列化为字节码 type 表里的条目。
- **varint（LEB128）**：一种变长整数编码，是本讲几乎所有字段的底层编码。下面 4.2 会复习。

一个关键直觉先建立起来：**字节码是「自描述的表格化容器」**。它不是一条裸指令流，而是由若干带 tag 的 section 组成；类型、字符串、常量都被抽出来单独建表，指令流里只放「表里的 id」。这样做的好处是去重与紧凑——同一个类型在成百上千条指令里出现，只在 Type 表里存一份。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/cuda/tile/_bytecode/version.py` | 定义 `BytecodeVersion` 枚举（`V_13_1/2/3/4`）与 `major()`/`minor()`/`tag()`/`as_string()`。 |
| `src/cuda/tile/_bytecode/writer.py` | **本讲主战场之一**：顶层 `write_bytecode` 容器、`_write_header`（magic）、`_Section` 枚举、`_section`/`_pad_to`/`_write_table` 通用机制、`BytecodeWriter`/`GlobalSection`/`FunctionBuilder`。 |
| `src/cuda/tile/_bytecode/basic.py` | `encode_varint`、`Table` 基类、`StringTable`、`encode_int_list`。 |
| `src/cuda/tile/_bytecode/type_base.py` | `TypeId`、`encode_typeid`、`encode_sized_typeid_seq`、`_TypeTableBase`、`PaddingValue`/`PtrAttr`。 |
| `src/cuda/tile/_bytecode/type.py` | `SimpleType`/`_CompositeType` 枚举、`TypeTable`（含 `tile`/`pointer`/`function` 等构造方法，带版本门控）。 |
| `src/cuda/tile/_bytecode/encodings.py` | 全部 `encode_*Op` 操作编码函数，以及 `RoundingMode`/`MemoryScope`/`AtomicRMWMode` 等枚举。 |
| `src/cuda/tile/_bytecode/constant.py` | `ConstantTable`（dense 常量表，常量嵌入用）。 |
| `src/cuda/tile/_bytecode/__init__.py` | 子包对外导出，并定义 `DYNAMIC_SHAPE`。 |
| `src/cuda/tile/_compile.py` | `_get_max_supported_bytecode_version`（探针式版本探测）、`_SUPPORTED_VERSIONS`、`parse_bytecode_version`、`CUDA_TILE_DUMP_BYTECODE` 落盘逻辑。 |
| `src/cuda/tile/_debug.py` | 读取 `CUDA_TILE_DUMP_BYTECODE` 环境变量。 |

## 4. 核心概念与源码讲解

### 4.1 BytecodeVersion：版本模型、特性门控与最大版本探测

#### 4.1.1 概念说明

TileIR 字节码是一个会随 CUDA Toolkit 演进的格式——几乎每个 CTK 小版本（13.1 / 13.2 / 13.3）都会往里加新操作、给老操作加新字段。为了让「用新版 cuTile 编译、用旧版 `tileiras` 执行」或反过来时不至于静默出错，字节码在**文件头里写明版本**，并在**编码端按版本门控每个特性**。

`BytecodeVersion` 就是这把「版本尺子」。它是一个 `IntEnum`，把 `13.3` 这样的语义版本编成一个可比较、可排序的整数。

#### 4.1.2 核心流程

版本整数采用 `major * 10000 + minor * 100 + tag` 的打包方式：

\[
\text{value} = \text{major}\times 10000 + \text{minor}\times 100 + \text{tag}
\]

于是 `V_13_1 = 130100`、`V_13_2 = 130200`、`V_13_3 = 130300`，dev 版 `V_13_4 = 130400`。这种打包保证了「数值大小 == 版本新旧」，所以直接用 `>=` 比较即可门控：

```text
encode_XXX(code_builder, ..., 某新字段):
    if code_builder.version >= BytecodeVersion.V_13_3:
        写入新字段        # 13.3+ 才编码
    else:
        assert 某新字段 == 默认值   # 旧版本根本表达不了，强制取默认
```

因为 `BytecodeVersion` 是 `IntEnum`，`code_builder.version >= BytecodeVersion.V_13_3` 实际比较的是 `130300` 这样的整数，天然单调。

最大支持版本的探测发生在编译入口：当用户没显式指定 `bytecode_version` 时，cuTile 会**用每个版本各写一个「空字节码」去喂 `tileiras`**，从高到低试，第一个能成功编译的版本就是当前工具链支持的最高版本。

#### 4.1.3 源码精读

版本枚举本身极简，值就是 `major*10000 + minor*100 + tag`：

[`_bytecode/version.py:8-23` —— `BytecodeVersion` 与 major/minor/tag 拆解](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/version.py#L8-L23) `major()` 整除 10000、`minor()` 取百位、`tag()` 取个位及十位；`as_string()` 在 tag 为 0 时返回 `"13.3"`、否则返回 `"13.3.1"`。

生产环境只允许三个稳定版本，`V_13_4` 仅 dev 可用：

[`_compile.py:713-721` —— `_SUPPORTED_VERSIONS` 与 `_all_bytecode_versions`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L713-L721) `dev_features_enabled()` 打开时才会把 `V_13_4` 也纳入候选。

最大版本探测是「写空字节码 + 实跑 `tileiras`」的暴力探针，从高到低取第一个能编过 SM 120 的版本：

[`_compile.py:724-748` —— `_get_max_supported_bytecode_version` 探针](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L724-L748) 注意它用 `@cache`，每个 `temp_dir` 只探测一次；探测体就是 `with bc.write_bytecode(num_functions=0, buf=probe, version=version): pass`——一个零函数的最小字节码。全部失败时回退到 `V_13_1` 并发 warning。

字符串到版本的解析用于显式指定时（如配置项）：

[`_compile.py:435-439` —— `parse_bytecode_version`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L435-L439) 按 `as_string()` 反查，找不到就报错并列出支持列表。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解「数值大小 == 版本新旧」这一设计的后果。
2. **步骤**：在 REPL 里执行
   ```python
   from cuda.tile._bytecode.version import BytecodeVersion as V
   print(int(V.V_13_1), int(V.V_13_3), V.V_13_3 >= V.V_13_1)
   print(V.V_13_3.as_string(), V.V_13_3.major(), V.V_13_3.minor(), V.V_13_3.tag())
   ```
3. **观察**：`130100 130300 True`，且 `as_string()` 给出 `"13.3"`。
4. **预期**：你会确认，正是因为值是 `major*10000+minor*100+tag`，整数比较与语义版本比较完全一致——这就是后面所有 `>= BytecodeVersion.V_13_X` 门控能成立的基础。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `V_13_4` 是 `130400` 而不是 `1304`？如果改成 `1304`，`V_13_4 >= V_13_3` 还成立吗？
  - **答**：`1304 < 130300`，比较会**反向**，门控逻辑全部失效。百位必须留给 minor、千/万位留给 major，才能保证「数值大 == 版本新」。
- **练习 2**：`tag()` 非 0 的版本，`as_string()` 会输出什么？这种版本在 `_SUPPORTED_VERSIONS` 里吗？
  - **答**：输出形如 `"13.3.1"`（三段）。`_SUPPORTED_VERSIONS` 里全是 tag=0 的稳定版，带 tag 的修订版只可能出现在 dev 构建里。

---

### 4.2 write_bytecode：顶层容器、section 序列与通用表编码

#### 4.2.1 概念说明

一个 `.tileirbc` 文件由三部分组成：

1. **header（文件头）**：magic 魔数 + 版本号，让任何读取器一眼认出「这是 TileIR 字节码、哪个版本」。
2. **若干 section（段）**：每段带一个 tag 标明身份，正文长度自描述。section 之间靠 tag 区分，**理论上可乱序**，但本 writer 固定按 Func → Global → Constant → Debug → Type → String 顺序写。
3. **EndOfBytecode 结束标记**：一个 tag=0 的哨兵字节，告诉读取器「后面没了」。

把类型、字符串、常量从指令流里抽出来**单独建表**，是这类字节码格式的核心设计：指令流里只引用 id（一个小 varint），真正的数据集中在表里，既紧凑又便于去重。三张表（Type / String / Constant）共享同一套编码机制 `_write_table`。

底层整数编码统一用 **varint（LEB128）**：每 7 位一组，最高位置 1 表示「还有后续字节」。小数字（0~127）只要 1 字节，这正是大多数 id、长度、opcode 的常态。

#### 4.2.2 核心流程

整体写入顺序如下（`write_bytecode` 编排）：

```text
_write_header(buf, version)               # magic + 版本
_section(Func,    align=8): varint(num_functions) + 各函数体
_write_global_section(...)                # 有 global 才写，align=1
_section(Constant, align=8): 常量表
_write_debug_info_section(...)            # align=8
_section(Type,     align=4): 类型表
_section(String,   align=4): 字符串表
buf.append(EndOfBytecode)                 # 0x00 哨兵
```

每个 section 的物理布局是：

```text
[ 1 字节: section_id | (0x80 if 对齐>1) ]
[ varint: section 正文长度 ]
[ 若对齐>1: varint(对齐) + 0xcb 填充到对齐边界 ]
[ section 正文 ]
```

其中 `_section` 是个上下文管理器：先在一个临时 `bytearray` 里收集正文，退出时再把「tag + 长度 + 可选对齐 + 正文」追加到主缓冲——这样正文的长度可以提前算出来。`_pad_to` 用 `0xcb` 作填充字节。

通用表 `_write_table` 的布局（Type/String/Constant 三张表都用它）：

```text
[ varint: 项数 N ]
[ 填充到 index_size 字节对齐 ]
[ N × index_size 字节的小端整数: 每项数据在数据区的偏移 ]
[ 数据区: 各项编码字节首尾拼接 ]
```

其中 `index_size` 对 Type/String 是 4、Debug 内部是 8。项按 id 排序后写入，偏移从 0 累加。

varint 编码本身：

```text
def encode_varint(x):           # x >= 0
    每次取低 7 位，若还有高位则置最高位(0x80)后输出，右移 7 位继续
    最后剩余部分（高位为 0）直接输出，不带 0x80
```

#### 4.2.3 源码精读

header 写入 magic `\x7fTileIR\x00`（8 字节）+ 主版本 1 字节 + 次版本 1 字节 + tag 2 字节小端：

[`_bytecode/writer.py:136-140` —— `_write_header` 写 magic 与版本](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py#L136-L140) 对 `V_13_3` 来说，header 12 字节就是 `7f 54 69 6c 65 49 52 00 0d 03 00 00`（`0x7f` + `"TileIR"` + `\x00` + major=13 + minor=3 + tag=0 两字节）。

section 的 tag 枚举：

[`_bytecode/writer.py:143-151` —— `_Section` 枚举](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py#L143-L151) 注意 `EndOfBytecode=0x00`、`Func=0x02`、`Type=0x05`、`Global=0x06`，值并非严格按出现顺序，靠 tag 区分而非位置。

顶层编排：

[`_bytecode/writer.py:109-133` —— `write_bytecode` 容器入口](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py#L109-L133) 先 header，再 Func 段（把 `num_functions` 写进段首、`yield BytecodeWriter` 让调用方填充函数体），随后依次 Global/Constant/Debug/Type/String，最后 `buf.append(_Section.EndOfBytecode._value_)`。

单段收集与对齐填充：

[`_bytecode/writer.py:154-167` —— `_pad_to` 与 `_section`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py#L154-L167) tag 字节用「id | 0x80」标记「本段带对齐」；对齐段会先写 varint(对齐值) 再补 `0xcb` 填充。`0xcb` 这个值是故意选的「不可能是正常数据」的填充字节。

通用表写入：

[`_bytecode/writer.py:210-225` —— `_write_table` 项数+偏移数组+数据区](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py#L210-L225) 先按 id 排序、写项数、写每个 item 的偏移（小端 `index_size` 字节），最后拼接所有 item 的编码字节。`assert expected_id == table._unwrap_id(id)` 校验 id 连续无空洞。

varint 与表的基类：

[`_bytecode/basic.py:37-42` —— `encode_varint`（LEB128 无符号）](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/basic.py#L37-L42) 循环把每 7 位带 `0x80` 续位输出；`assert x >= 0` 表明它只编码非负数。

[`_bytecode/basic.py:13-34` —— `Table` 基类与 `StringTable`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/basic.py#L13-L34) `Table` 继承 `dict`，`__missing__` 在首次查一个键时自动分配「`len(self) + _starting_id`」作为 id——这就是**自增去重**的核心：同一个字符串/类型/常量第二次查会命中已有 id，绝不重复分配。

常量表与之同构：

[`_bytecode/constant.py:15-25` —— `ConstantTable.dense_constant`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/constant.py#L15-L25) 每个 dense 常量的编码是 `varint(字节数) + 原始字节`，整体作为表键；常量嵌入（`Constant` 参数、`ct.full` 初值等）最终都引用这里的 `constant_id`。

#### 4.2.4 代码实践（实操型·待本地验证）

1. **目标**：亲手生成一个最小字节码，用十六进制看清 header 与 section 布局。
2. **步骤**：在仓库根目录的 Python 里执行
   ```python
   from cuda.tile._bytecode import write_bytecode, BytecodeVersion
   buf = bytearray()
   with write_bytecode(num_functions=0, buf=buf, version=BytecodeVersion.V_13_3) as w:
       pass  # 零函数最小字节码，正是版本探针用的那种
   print(buf.hex(' '))
   ```
3. **观察**：开头应是 `7f 54 69 6c 65 49 52 00 0d 03 00 00`（magic + 版本 13.3）；随后是 Func 段 tag（`02 | 0x80 = 82`，因为对齐 8）+ 长度 varint + 对齐填充，段内首字节是 `00`（`num_functions=0`）；中部依次出现 Constant(`84`)/Debug(`83`)/Type(`85`)/String(`81`) 各段；结尾一个 `00`（EndOfBytecode）。
4. **预期**：你能逐字节把 hex 输出与 4.2.2 的布局对上。**待本地验证**：实际偏移与填充字节数以你机器上的输出为准。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 Func 段的 tag 字节是 `0x82` 而 String 段是 `0x81`？
  - **答**：Func 对齐 8（`alignment > 1`），tag = `Func(0x02) | 0x80 = 0x82`；String 对齐 4 但 `0x80` 标志只在「对齐 > 1」时置位——而 String 的对齐是 4（也 > 1），所以也带 `0x80`，tag = `0x01 | 0x80 = 0x81`。区别仅在 id 不同。
- **练习 2**：`_write_table` 里为什么先写「每个 item 的偏移」再写数据区，而不是直接逐项「长度+数据」？
  - **答**：偏移数组定长（每项 `index_size` 字节），读取器可以**先读偏移表、随机跳到任意 item**，而不必顺序扫描；这也让数据区可以紧凑拼接、共享对齐，是典型「索引 + 数据分离」的二进制布局。

---

### 4.3 BytecodeWriter：函数体写入器与各张表

#### 4.3.1 概念说明

`write_bytecode` 只搭好「容器骨架」，真正往 Func 段里填函数体、并维护 Type/String/Constant/Debug 四张表的是 `BytecodeWriter`。它的角色是**一次编译会话的「中央账本」**：

- Func 段正文直接写进它的 `_buf`（其实就是 Func section 的 `section_buf`）。
- 它持有 `_type_table` / `_string_table` / `_constant_table` / `_debug_attr_table` 四张表，所有函数体共享同一份表，从而跨函数去重。
- `_global_section` 负责全局变量定义（如有）。

每写一个函数，就走一次 `writer.function(...)` 上下文管理器：它写出函数头（名字 id、函数类型 id、entry 标志、debug 索引），然后**把函数体的编码交给一个独立的 `CodeBuilder`**（拥有自己的 `buf`），最后把 `CodeBuilder.buf` 的长度与内容并进主缓冲。这个「每个函数一个独立 `CodeBuilder`」正是 u7-l1 讲过的「每个函数获得独立 `CodeBuilder.buf`」。

#### 4.3.2 核心流程

```text
writer = BytecodeWriter(func_section_buf, version)
writer.function(name, param_types, result_types, entry_point, hints, debug_attr):
    num_functions += 1
    写 varint(name 的 string_id)          # 名字进 String 表，这里只存 id
    写 typeid(函数类型)                    # 函数类型进 Type 表，这里只存 id
    写 1 字节 flags: (0x02 if entry_point) | (0x04 if hints)
    写 varint(debug_info 的下标)
    若 entry_point 且有 hints: 写 entry hints
    创建独立 CodeBuilder(buf=新 bytearray, ...)
    yield FunctionBuilder(code_builder, 入口参数 Value 元组)
    # --- 调用方（ir2bytecode）在此期间往 code_builder.buf 里填指令 ---
    写 varint(len(code_builder.buf))       # 函数体长度
    主缓冲.extend(code_builder.buf)         # 函数体正文
```

`FunctionBuilder` 是个 `NamedTuple`，把 `code_builder` 与「入口参数 Value 元组」一起交给调用方——入口参数对应的 `Value` id 是预留的（`_make_value_tuple(len(parameter_types))`），保证函数体内引用入口参数时 id 正确。

#### 4.3.3 源码精读

`BytecodeWriter` 持有四张表与 global 段，版本透传给 `TypeTable`：

[`_bytecode/writer.py:52-62` —— `BytecodeWriter.__init__`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py#L52-L62) `_type_table = TypeTable(version)`——类型表需要知道版本才能按版本编码（见 4.4）。

函数写入器：写头 + 委托 CodeBuilder + 拼接：

[`_bytecode/writer.py:80-106` —— `BytecodeWriter.function`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py#L80-L106) 注意函数类型 `sig_ty = self._type_table.function(parameter_types, result_types)` 会把函数类型本身也登记进 Type 表；`builder._make_value_tuple(len(parameter_types))` 为入口参数预留连续的 value id。

`FunctionBuilder` 与 `GlobalSection`：

[`_bytecode/writer.py:19-49` —— `FunctionBuilder` 与 `GlobalSection`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/writer.py#L19-L49) `GlobalSection.define_global` 在 13.3+ 额外写 `symbol_visibility` 与 `constant` 标志（又一个版本门控的实例）。

子包导出汇总了这些组件，并定义了「动态形状」哨兵：

[`_bytecode/__init__.py:5-17` —— 子包导出与 `DYNAMIC_SHAPE`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/__init__.py#L5-L17) `DYNAMIC_SHAPE = -1 << 63`（`INT64_MIN`）用于在类型 shape 里标记「这一维是运行时动态的」，与静态形状特化（u3-l7）配合。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：确认「四张表跨函数共享」与「每函数独立 CodeBuilder」。
2. **步骤**：读 `_ir2bytecode.py` 里 `generate_bytecode_for_kernel` 对 `writer.function(...)` 的调用（u7-l1 已跟踪过），再回到 `writer.py:80-106` 看 `CodeBuilder` 的 `buf=bytearray()` 是每次 `function` 调用新建的。
3. **观察**：`_type_table`/`_string_table`/`_constant_table` 是 `BytecodeWriter` 的实例字段，整个 `write_bytecode` 生命周期只建一次；而 `CodeBuilder` 每个 function 新建一个。
4. **预期**：你会得出结论——同一个 `float32` 类型即使被 N 个函数用，也只在 Type 表里占一条；而每个函数的指令流互不干扰。

#### 4.3.5 小练习与答案

- **练习 1**：函数头里写的是「函数类型 id」，而不是直接写参数类型列表。这样设计有什么好处？
  - **答**：函数类型本身也是 Type，复用同一张 Type 表去重；多个签名相同的函数共享一个类型条目。读取器只要按 id 查表即可还原参数/结果类型。
- **练习 2**：`function` 里 `self._buf.append(... flags ...)` 用的是 `append`（单字节），而名字用的是 `encode_varint`。为什么 flags 是定长 1 字节？
  - **答**：flags 是一个固定含义的位集（`entry_point`、`hints` 几个布尔），值域很小且位置固定，定长 1 字节便于读取器按位解码，不需要 varint 的变长开销。

---

### 4.4 TypeTable：类型去重与版本化类型编码

#### 4.4.1 概念说明

字节码里的「类型」分两类：

- **简单类型**（`SimpleType`）：`I1/I8/.../I64`、`F16/F32/F64`、`BF16/TF32`、各种 `float8`、`Token` 等，每个用一个单字节 tag 编码（如 `F32 = b"\x07"`）。
- **复合类型**（`_CompositeType`）：`Pointer/Tile/TensorView/PartitionView/Function/GatherScatterView/StridedView`，由「tag + 子类型/形状/步长等」组成。

`TypeTable` 用「**类型的编码字节串**作键、**自增整数 id** 作值」实现去重：两次构造完全相同的 `tile<f32, 4x8>` 会命中同一个 id。指令流与函数头里只放这个 id（一个小 varint）。

类型编码**会随版本变化**：例如 `PartitionView` 在 13.3 起改用「统一位域」编码可选的 `padding_value`；`Pointer`/`TensorView` 的 `ptrAttr` 字段要到 13.4 才能编码。这些门控就在 `TypeTable` 的各构造方法里。

#### 4.4.2 核心流程

类型表建立流程：

```text
TypeTable.__init__: _predefine(I1, id=0); _predefine(I32, id=1)   # 头两个 id 固定
之后每次 type_table.tile(elementType, shape):
    编码字节串 key = b"\x0d" + varint(elementType.id) + int_list(shape, 8字节)
    return self[key]   # 首次分配新 id，之后命中
```

`TypeId` 的写入极其简单：

```text
encode_typeid(type_id, buf):  encode_varint(type_id.type_id, buf)
encode_sized_typeid_seq(ids, buf): varint(len) + 每个 id 的 varint
```

注意 `_write_table` 写到文件时，每项的「数据」就是这个编码字节串 key（对简单类型就是单字节 tag，对复合类型是 tag+后续）。

#### 4.4.3 源码精读

简单类型与复合类型的 tag 字典：

[`_bytecode/type.py:14-40` —— `SimpleType` 与 `_CompositeType`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/type.py#L14-L40) 注释标出每个类型的最低版本（如 `F8E8M0FNU` since 13.2、`F4E2M1FN`/`I4`/`GatherScatterView`/`StridedView` since 13.3）——这些是「该类型能否在目标版本出现」的隐式约束。

`TypeTable` 预定义头两个 id：

[`_bytecode/type.py:48-64` —— `TypeTable.__init__` 预定义 I1=0/I32=1](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/type.py#L48-L64) `_predefine` 用 `__missing__` 分配 id 后校验 `== expected_id`，保证 I1/I32 永远是 0/1，这是与读取器的硬契约。

复合类型的版本化编码——以 `tile`（无版本差异）与 `pointer`（13.4 才支持 ptrAttr）对比：

[`_bytecode/type.py:96-100` —— `TypeTable.tile` 拼装键](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/type.py#L96-L100) `tile` = `0x0d` + varint(元素类型 id) + int_list(shape，每维 8 字节有符号小端)。

[`_bytecode/type.py:78-94` —— `TypeTable.pointer` 的版本门控](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/type.py#L78-L94) 在 < 13.4 时若 `ptrAttr != Missing` 直接 `raise ValueError`——即「旧版本字节码无法表达该属性」。这正是 4.1 说的「旧版本下断言/报错」式门控在类型层的体现。

`partition_view` 展示了「13.3 起改用统一位域」的演进：

[`_bytecode/type.py:126-146` —— `TypeTable.partition_view` 的两种编码](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/type.py#L126-L146) `use_unified_bitfield = version >= V_13_3`：新版本先写 `optional_flags` 位域、末尾写 `padding_value` 字节；旧版本写「是否非默认」的 varint + 字节。同一语义、两种物理布局。

`TypeId` 与序列化辅助：

[`_bytecode/type_base.py:33-40` —— `encode_typeid` 与 `encode_sized_typeid_seq`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/type_base.py#L33-L40) 都只是 varint；`TypeId` 是个冻结 dataclass，仅持一个 `type_id: int`。

#### 4.4.4 代码实践（实操型·待本地验证）

1. **目标**：直观看到类型去重与 id 分配。
2. **步骤**：
   ```python
   from cuda.tile._bytecode.type import TypeTable, SimpleType
   from cuda.tile._bytecode.version import BytecodeVersion
   tt = TypeTable(BytecodeVersion.V_13_3)
   f32 = tt.simple(SimpleType.F32)
   t1 = tt.tile(f32, [4, 8])
   t2 = tt.tile(f32, [4, 8])     # 完全相同
   t3 = tt.tile(f32, [4, 4])     # shape 不同
   print(f32.type_id, t1.type_id, t2.type_id, t3.type_id)
   ```
3. **观察**：`0 1 2 2 3`（I1=0、I32=1、F32=2、`tile<f32,4x8>`=3，`t2` 命中 3，`t3` 分配 4）。具体数值**待本地验证**。
4. **预期**：`t1 is t2`（同一 `TypeId`），证明同形 tile 只占一条表项；`t3` 不同则新分配。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 I1 和 I32 要被「预定义」到固定 id 0 和 1，而 F32 不用？
  - **答**：I1（布尔）与 I32（默认整数）在指令里出现极频繁，且很多 op 的「控制」结果（如 `i1` 条件）写死依赖固定 id。预定义保证它们永远是 0/1，读取器可以硬编码这些常用类型，省去查表。
- **练习 2**：在 13.2 的 `TypeTable` 上调用 `pointer(pointee, PtrAttr.Unicast)` 会发生什么？为什么？
  - **答**：抛 `ValueError("parameter 'ptrAttr' requires bytecode version 13.4+...")`。因为旧版本字节码格式里根本没有存放 `ptrAttr` 的位置，无法忠实序列化，编译器选择「显式报错」而非「静默丢弃」。

---

### 4.5 encode_*Op：单条操作的编码套路与版本门控

#### 4.5.1 概念说明

`encodings.py` 里有上百个 `encode_XXXOp` 函数，乍看眼花缭乱，但它们**共享同一套固定模板**。掌握模板，任何一个 op 都能秒读。模板里的字段按固定顺序出现：

1. **Opcode（操作码）**：一个 varint，唯一标识 op 种类（如 `AddFOp=2`、`ForOp=41`、`YieldOp=109`）。
2. **Result types（结果类型）**：单结果用 `encode_typeid(result_type)`；变长结果（如 `ForOp` 的多个归纳结果、`BreakOp`）用 `encode_sized_typeid_seq(...)`；无结果（如 `ReturnOp`）写一个空 seq。
3. **Flags（标志位，可选）**：把若干布尔打包成一个 varint（按位 `|`），常用于「这个可选字段在不在」。
4. **Attributes（属性，可选）**：编译期常量参数，如 `RoundingMode`、`dim`、`message` 字符串，通过 `code_builder.encode_opattr_*` 系列写入（最终多引用 String/Constant 表的 id）。
5. **Operands（操作数，可选）**：对其他 `Value` 的引用，就是 `encode_varint(value.value_id)`；可选操作数用 `encode_optional_operand`（在则写 id）、变长用 `encode_sized_variadic_operands`（先写个数再写各 id）。
6. **`code_builder.new_op(num_results)`**：认领结果 value id，返回 `Value` 或 `Value` 元组。

版本门控在这里最密集，主要有两种写法：

- **「新字段只在 X+ 写」**：`if version >= V_13_X: 写新字段 else: assert 取默认值`——典型如 `ForOp` 的 `unsignedCmp`、`NegIOp` 的 `IntegerOverflow`。
- **「整条 op 只在 X+ 存在」**：函数名注释直接标 `# since 13.3`，opcode 用高位编号（如 `AllocaOp=113`、`MmaFScaledOp=114`），调用方负责不在旧版本触发。

#### 4.5.2 核心流程

最简模板（无 flags、无 attributes，单结果）——以 `AbsFOp` 为例：

```text
encode_AbsFOp(cb, result_type, source):
    encode_varint(0, cb.buf)              # opcode
    encode_typeid(result_type, cb.buf)    # 结果类型
    encode_operand(source, cb.buf)        # 操作数（source.value_id 的 varint）
    return cb.new_op()                    # 认领 1 个结果
```

带 flags + attributes 的模板——以 `AddFOp` 为例：

```text
encode_AddFOp(cb, result_type, lhs, rhs, rounding_mode, flush_to_zero):
    encode_varint(2, cb.buf)              # opcode
    encode_typeid(result_type, cb.buf)    # 结果类型
    encode_varint(bool(flush_to_zero), cb.buf)        # flags
    cb.encode_opattr_enum(RoundingMode, rounding_mode)# attribute（枚举字节）
    encode_operand(lhs, cb.buf); encode_operand(rhs, cb.buf)  # operands
    return cb.new_op()
```

版本门控模板——以 `ForOp` 的 `unsignedCmp` 为例：

```text
_flag_bits = bool(unsignedCmp)
assert _flag_bits < 1 or cb.version >= V_13_2     # 旧版本只能取 False
if cb.version >= V_13_2:
    encode_varint(_flag_bits, cb.buf)             # 13.2+ 才写这个 flag
```

`new_op` 的分配逻辑：

```text
new_op(num_results=None):
    debug_attr_per_op.append(cur_debug_attr); num_ops += 1
    None  → 分配 1 个 Value(next_value_id++)
    0     → 返回 None（无结果 op）
    k>0   → 分配连续 k 个 Value
```

#### 4.5.3 源码精读

最简模板——`AbsFOp`（opcode 0，仅结果类型 + 一个操作数）：

[`_bytecode/encodings.py:118-130` —— `encode_AbsFOp` 最简形态](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L118-L130) 这就是「opcode + result type + operand」的骨架，对照阅读其余所有 `encode_*Op` 都能套上。

flags + attributes 模板——`AddFOp`：

[`_bytecode/encodings.py:148-168` —— `encode_AddFOp` flags+attributes+operands](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L148-L168) `flush_to_zero` 进 flags、`rounding_mode` 进 attributes（经 `encode_opattr_enum` 写入枚举的原始字节，如 `NEAREST_EVEN=b"\x00"`）。

版本门控——`ForOp`（13.2 加 `unsignedCmp` flag）与 `NegIOp`（13.2 加 `IntegerOverflow` 属性）：

[`_bytecode/encodings.py:823-848` —— `encode_ForOp` 的 13.2 flag 门控](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L823-L848) `assert _flag_bits < 1 or cb.version >= V_13_2` 是「旧版本下断言取默认值」式门控的标准写法。

[`_bytecode/encodings.py:1649-1667` —— `encode_NegIOp` 的 13.2 属性门控](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L1649-L1667) 13.2+ 才写 `IntegerOverflow` 属性，否则 `assert overflow == NONE`。

13.3 / 13.4 的新增 op 与字段——`GlobalOp`（13.3 加 `constant` flag 与 `SymbolVisibility` 属性）与 `LoadViewTkoOp`（13.4 加 `inbounds` 布尔数组）：

[`_bytecode/encodings.py:960-984` —— `encode_GlobalOp` 的 13.3 门控](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L960-L984) 新版本多写一个 flag（`constant`）和一个属性（`symbol_visibility`）；旧版本 `assert symbol_visibility == Public`。

[`_bytecode/encodings.py:1123-1161` —— `encode_LoadViewTkoOp` 的 13.4 inbounds 门控](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/encodings.py#L1123-L1161) 13.4+ 写 `encode_opattr_dense_bool_array(inbounds)`；旧版本只能表达「全 false」，否则报错。注意它的 flags 是个五位打包（`memory_scope | optimization_hints<<1 | token<<2`…）。

`new_op` 与操作数编码——`CodeBuilder` 里：

[`_bytecode/code_builder.py:69-79` —— `CodeBuilder.new_op` 认领结果 id](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py#L69-L79) `None`/`0`/`k>0` 三种分支决定返回单 `Value`、`None` 还是 `Value` 元组；`next_value_id` 单调递增，呼应 SSA。

[`_bytecode/code_builder.py:143-159` —— operand 编码辅助函数](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_bytecode/code_builder.py#L143-L159) `encode_operand = encode_varint(value_id)`；可选操作数「在则写」、变长操作数「先个数后各项」。

#### 4.5.4 代码实践（实操型·待本地验证）

1. **目标**：手动编码一条 `AddFOp`，与内置实现对照，验证模板理解。
2. **步骤**：
   ```python
   from cuda.tile._bytecode.code_builder import CodeBuilder, Value
   from cuda.tile._bytecode.type import TypeTable
   from cuda.tile._bytecode.encodings import encode_AddFOp, RoundingMode
   from cuda.tile._bytecode.basic import StringTable
   from cuda.tile._bytecode.constant import ConstantTable
   from cuda.tile._bytecode.version import BytecodeVersion

   tt = TypeTable(BytecodeVersion.V_13_3)
   f32 = tt.F32
   builder = CodeBuilder(buf=bytearray(), version=BytecodeVersion.V_13_3,
                         string_table=StringTable(),
                         constant_table=ConstantTable(),
                         debug_attr_per_op=[])
   a = builder.next_value_id; builder.next_value_id += 1   # 手造 Value 0
   b = builder.next_value_id; builder.next_value_id += 1   # 手造 Value 1
   encode_AddFOp(builder, f32, Value(a), Value(b),
                 RoundingMode.NEAREST_EVEN, flush_to_zero=False)
   print(builder.buf.hex(' '))
   # 期望: opcode(02) + typeid(f32) + flag(00) + rounding(00) + operand(00) + operand(01)
   ```
3. **观察**：输出应类似 `02 02 00 00 00 01`（`02`=AddFOp opcode；`02`=F32 的 type id；`00`=flush_to_zero False；`00`=NEAREST_EVEN；`00`/`01`=两个操作数 id）。具体 type id **待本地验证**（取决于 F32 被分配到的 id）。
4. **预期**：你能把输出的每个字节与 4.5.2 的模板逐项对上，说明已掌握编码套路。

#### 4.5.5 小练习与答案

- **练习 1**：`encode_ReturnOp` 和 `encode_AbsFOp` 都没有 flags/attributes，但它们的「结果类型」写法不同。分别是什么？为什么？
  - **答**：`AbsFOp` 单结果，写 `encode_typeid(result_type)`；`ReturnOp` 无结果，写 `encode_sized_typeid_seq(())`（一个「项数=0」的 varint）。前者固定一个类型 id，后者用变长 seq 是因为 return 可带任意个数返回值（这里 0 个）。
- **练习 2**：`encode_ForOp` 里那行 `assert _flag_bits < 1 or code_builder.version >= BytecodeVersion.V_13_2` 想防住什么？
  - **答**：防住「在 13.1 字节码上编码一个 `unsignedCmp=True` 的循环」。13.1 格式里没有存放该 flag 的位，若强行写会破坏布局或被读取器误解，所以用断言强制「旧版本只能取默认值 False」，把不兼容在编码期就拦下。

---

## 5. 综合实践

把本讲全部最小模块串起来：**dump 一个真实内核的 `.tileirbc`，逐段拆解它的二进制结构，并在其中定位一条具体指令的编码。**

1. **准备**：在仓库根目录写一个最小内核并设置 dump 环境变量。
   ```python
   # dump_bytecode.py
   import os
   os.environ["CUDA_TILE_DUMP_BYTECODE"] = "/tmp/tileirbc_dump"
   import cuda.tile as ct
   import torch

   @ct.kernel
   def add_kernel(a: ct.Tensor, b: ct.Tensor, c: ct.Tensor, n: ct.Constant[int]):
       bid = ct.bid(0)
       x = ct.load(a, (bid,), (32,))
       y = ct.load(b, (bid,), (32,))
       ct.store(c, (bid,), x + y)

   a = torch.ones(64, dtype=torch.float32, device="cuda")
   b = torch.ones(64, dtype=torch.float32, device="cuda")
   c = torch.empty(64, dtype=torch.float32, device="cuda")
   ct.launch(0, (2,), add_kernel, [a, b, c, 64])
   ```
2. **运行**后去 `/tmp/tileirbc_dump/` 找到生成的 `.tileirbc` 文件（文件名由 `unique_path_from_func_desc` 生成）。
3. **hexdump**：用 `xxd` 或 `hexdump -C` 查看前若干字节。
4. **逐段拆解**，对照 `writer.py` 解释：
   - header：前 12 字节是否 `7f 54 69 6c 65 49 52 00 0d XX 00 00`？`XX` 是探测到的 minor（应为 1/2/3 之一）。
   - Func 段：tag 字节（`02 | 0x80 = 82`）、varint 长度、对齐填充（`cb`）；段内首字节 `01`（`num_functions=1`）。
   - 函数体：找函数头里的 name string id、函数 type id；随后是 `code_builder.buf` 的长度 varint + 指令流。
   - 在指令流里找 `AddFOp`：opcode 字节 `02`，后面跟结果 type id、`00`(flush_to_zero)、`00`(NEAREST_EVEN)、两个操作数 value id。
   - 跳到末尾，确认最后一个字节是 `00`（EndOfBytecode）。
   - 在文件中后段定位 Type/String/Constant 各表的 tag（`85`/`81`/`84`）。
5. **交叉验证**：把你在指令流里找到的 `AddFOp` 结果 type id，去 Type 表数据区里数到对应条目，确认它就是 `F32`（编码字节 `07`）。
6. **预期产物**：一份「字节偏移 → 含义」的注释表，能向别人讲清「这个文件从头到尾每个字节段是什么」。**待本地验证**：实际偏移、id、填充字节数以你机器上的 dump 为准。

> 提示：dump 落盘逻辑在 [`_compile.py:493-499`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L493-L499)，环境变量由 [`_debug.py:11`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_debug.py#L11) 读取。

## 6. 本讲小结

- 一个 `.tileirbc` 文件 = **header（magic `\x7fTileIR\x00` + 版本）+ 若干带 tag 的 section + `EndOfBytecode` 哨兵**；section 顺序固定为 Func → Global → Constant → Debug → Type → String，靠 tag（`Func=0x02`、`Type=0x05`…）而非位置区分。
- **类型/字符串/常量被抽出来单独建表**，指令流只引用 id；三张表共享 `_write_table` 的「项数 + 偏移数组 + 数据区」布局；去重靠 `Table.__missing__` 的自增 id。
- `BytecodeVersion` 用 `major*10000+minor*100+tag` 打包成 `IntEnum`，「数值大小 == 版本新旧」，所以 `version >= V_13_X` 直接做特性门控；最大版本由「空字节码探针实跑 `tileiras`」探测（`_get_max_supported_bytecode_version`）。
- **版本门控有两套写法**：「新字段只在 X+ 写，旧版本断言取默认值」（如 `ForOp` 的 `unsignedCmp`、`GlobalOp` 的 `constant`）与「整条 op 只在 X+ 存在」（高位 opcode，如 `AllocaOp=113`）。
- 所有 `encode_*Op` 共享**固定模板**：opcode(varint) → result type(id 或 sized seq) → flags(打包 varint) → attributes(`encode_opattr_*`) → operands(`encode_operand`/optional/variadic) → `new_op(num_results)` 认领 value id。
- `TypeTable` 用「编码字节串作键」去重，I1/I32 预定义为 id 0/1；复合类型（`tile`/`pointer`/`partition_view`…）的编码本身也随版本演进（如 `partition_view` 13.3 起改用统一位域）。

## 7. 下一步学习建议

- **接 u7-l3**：本讲只到「字节码字节流」为止，这些字节最终是怎么变成 cubin 的？下一讲讲 `tileiras` 的定位（pip/PATH/CUDA_HOME 三级查找）、`compile_cubin` 的参数（`--gpu-name`/`-O`/`--lineinfo`）与 `get_sm_arch` 探测——也就是「字节码 → cubin」这一跳。
- **回看 u7-l1**：如果你对 `code_builder.buf` 里那些字节是怎么逐条产生的还不够熟，回头重读 `generate_bytecode_for_block` 与 `NestedBlockBuilder.new_block`（嵌套 region 的缓冲切换），结合本讲的「函数体长度 varint + 正文」会有新体会。
- **延伸阅读**：想理解「为什么 Type/String 表要分离索引与数据」可以类比 MLIR 的 bytecode 格式（cuTile 的 TileIR 与 MLIR 同源）；想理解 varint 可对比 protobuf 的 LEB128。
- **动手方向**：尝试在 dump 出的 `.tileirbc` 上，用本讲学到的布局手写一个「最小读取器」——只解析 header + 各 section tag + 三张表的项数，能正确报出版本号与段数即可，这会逼你把本讲的所有结构真正内化。
