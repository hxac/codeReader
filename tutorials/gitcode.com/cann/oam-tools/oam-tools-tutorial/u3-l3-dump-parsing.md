# u3-l3 Dump 文件解析：Python 与 C++/protobuf 协作

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `dump_data.proto` 中 `DumpData` 消息的字段构成，以及它与 OpInput/OpOutput/OpBuffer/Workspace 等子消息的关系。
2. 解释 `dump_proto_to_json.cpp` 如何被编译成动态库 `libascend_dump_parser.so`，以及它暴露的唯一 C 接口 `ParseDumpProtoToJson` 做了什么。
3. 追踪 `dump_data_parser.py` 中 Python 通过 **ctypes 直接加载 so 库**（而不是子进程、也不是 Python protobuf 包）调用 C++ 解析能力的那几行代码，理解整个 "二进制 → proto → JSON → numpy" 的链路。
4. 理解 `-dtype` 参数如何影响输出：它只在 `-d` 指向 `.bin` 文件时生效，把裸二进制按指定 dtype 转成 `.npy`。

## 2. 前置知识

- **Dump 文件**：昇腾算子执行时把输入、输出、workspace 等张量数据落盘形成的文件，是定位算子计算错误（如 AI Core Error）的第一手现场。本仓 `src/msaicerr/proto_parse/` 处理的是其中的"大 Dump"格式。
- **protobuf（Protocol Buffers）**：Google 的二进制序列化框架。先用 `.proto` 文件声明消息结构（message），再用 protoc 编译器生成代码，之后就能以紧凑的二进制格式读写这些消息。本讲只需理解 "proto 文件 = 数据结构的声明书" 即可。
- **ctypes**：Python 标准库，可以在运行期用 `ctypes.CDLL("libxxx.so")` 加载动态库并直接调用其中以 `extern "C"` 方式导出的函数。这是 Python 与 C++ 协作的一种最轻量的方式——不需要写 Python 扩展模块，也不需要起子进程。
- **numpy dtype（数据类型）**：`np.frombuffer`/`np.fromfile` 读出的裸字节本身没有类型，必须指定 dtype（如 float16、int32）才能被解释成有意义的数值。同一个 `.bin` 文件用不同 dtype 读，数值含义完全不同——这正是 `-dtype` 参数存在的原因。
- 承接 [u3-l1](u3-l1-msaicerr-entry.md)：`msaicerr.py` 的 `-d` 参数走 `convert_dump_data()`，本讲深入它调用的 `DumpDataParser`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/msaicerr/proto_parse/dump_data.proto` | 声明 Dump 文件头部（header）的数据结构：`DumpData` 及其嵌套消息、数据类型枚举、格式枚举 |
| `src/msaicerr/proto_parse/dump_proto_to_json.cpp` | C++ 解析实现：把 header 的 proto 二进制反序列化成 `DumpData`，再转成 JSON 写盘 |
| `src/msaicerr/proto_parse/dump_proto_to_json.h` | 对外导出的 C 接口声明 `ParseDumpProtoToJson` |
| `src/msaicerr/proto_parse/CMakeLists.txt` | 把 `.proto` 编译成 pb 代码，并连同 cpp 打成共享库 `libascend_dump_parser.so` |
| `src/msaicerr/ms_interface/dump_data_parser.py` | Python 侧主体：`DumpDataParser`（dtype 映射与落盘）与 `BigDumpDataParser`（ctypes 调 so 解析 header） |
| `src/msaicerr/msaicerr.py` | 入口：`-d`/`-dtype`/`-out` 参数定义与 `convert_dump_data()` 分发 |
| `docs/zh/msaicerr/Dump_files_parsing.md` | 用户视角的 Dump 解析使用说明 |

## 4. 核心概念与源码讲解

### 4.1 dump_data.proto：Dump 文件头部的"结构声明书"

#### 4.1.1 概念说明

"大 Dump" 文件在磁盘上是**两段式**布局：

```
+-------------------+--------------------------------------------+
| 8 字节 uint64     | header_length 字节的 proto 序列化数据      | 后面紧跟各张量的裸数据字节
| (header_length)   | (即一个 DumpData 消息)                     |
+-------------------+--------------------------------------------+
```

前 8 字节是一个小端无符号整数，声明 header 的长度；header 本体是一个按 protobuf 编码的 `DumpData` 消息，记录了"这个 Dump 里有哪些输入/输出/workspace、每个张量的 dtype、shape、大小"等**元数据**；文件剩余部分则是**张量数据本体**按 header 中声明的顺序逐个拼接。

`dump_data.proto` 就是 header 那段二进制的结构声明书。C++ 侧按它生成解析代码，Python 侧虽不直接解析 proto，但依赖 C++ 产出的 JSON——JSON 里的字段名就来自这份 proto。

#### 4.1.2 核心流程

proto 文件的阅读顺序建议自顶向下：

1. 先看顶层消息 `DumpData`（有哪些类别的数据）；
2. 再看每类数据对应的子消息 `OpInput` / `OpOutput` / `OpBuffer` / `Workspace`（每条记录长什么样）；
3. 最后看两个枚举 `OutputDataType` / `OutputFormat`（dtype 和排布格式的取值空间）。

#### 4.1.3 源码精读

顶层消息 `DumpData`，一个字段的遗漏都会导致解析端拿不到对应信息：

[dump_data.proto:171-181](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/dump_data.proto#L171-L181)
—— 定义 `DumpData` 消息：`version`、`dump_time`、`op_name`、`dfx_message` 四个标量字段，加上四个 repeated 容器：`output`（OpOutput 列表）、`input`（OpInput 列表）、`buffer`（OpBuffer 列表）、`space`（Workspace 列表）、`attr`（OpAttr 列表）。注意 Python 侧 `BigDumpDataParser.data_types = ['input', 'output', 'buffer', 'space']` 与这里的容器名一一对应。

[dump_data.proto:133-145](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/dump_data.proto#L133-L145)
—— `OpInput` 消息：算子输入张量的元数据。关键字段：`data_type`（OutputDataType 枚举）、`shape`（Shape 消息）、`size`（数据字节数，Python 侧按它从文件里读 столько 字节）、`input_type`（为 7 即 TILING_TYPE 时，该输入是 tiling 数据）、`address`/`offset`（设备侧地址信息）。

[dump_data.proto:119-131](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/dump_data.proto#L119-L131)
—— `OpOutput` 消息：算子输出张量元数据，比 OpInput 多了 `original_op`（溯源到原始框架算子）和 `dim_range`，少了 `input_type`。

[dump_data.proto:162-169](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/dump_data.proto#L162-L169)
—— `Workspace` 消息：带一个嵌套枚举 `SpaceType`（目前仅 LOG），对应 Python 侧 `parse_types` 里的 `'space'`（落盘时改名为 workspace）。

[dump_data.proto:4-46](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/dump_data.proto#L4-L46)
—— `OutputDataType` 枚举：dtype 的编号体系，从 DT_FLOAT=1 到 DT_FLOAT4_E1M2=40。**这份枚举与 Python 侧 `DATA_TYPE_TO_DTYPE_MAP` 的键严格对齐**（如 27=DT_BF16 ↔ '27': 'bfloat16'），最近新增的 float8/float6/float4 系列（34~42）正是为了适配 Ascend950 新 dtype。

#### 4.1.4 代码实践

1. **实践目标**：独立写出 `DumpData` 消息的字段清单，并验证 dtype 枚举与 Python 映射表对齐。
2. **操作步骤**：
   - 打开 `dump_data.proto`，只看 171-181 行，把 `DumpData` 的 9 个字段按"字段名 / 类型 / 编号"列成表；
   - 再打开 `dump_data_parser.py` 43-88 行的 `DATA_TYPE_TO_DTYPE_MAP`，逐个核对 `'27'`、`'34'`、`'39'` 等键对应的枚举值是否与 proto 中相同编号的 DT_ 名一致。
3. **需要观察的现象**：两个文件中编号与名称的对应关系完全一致，没有缺号。
4. **预期结果**：得到一张 9 行的 DumpData 字段表，以及一份 40 余项的 dtype 对照说明。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bytes data` 字段在 `OpInput`/`OpOutput` 里存在，而 Python 还要自己从文件里按 `size` 读数据？

**答案**：header 只包含 header_length 字节，张量数据本体在文件 header 之后的连续区域。`ParseDumpProtoToJson` 只把 header 段喂给 `ParseFromString`（见 4.3.3），所以反序列化出的 `data` 字段是空的；真正的数据字节由 Python 侧 `_parse_binary_to_json_data` 按 `size` 逐段从文件里读回并塞进 JSON 条目的 `data` 键。这样避免了几百 MB 张量数据经过 proto/base64 的双重膨胀。

**练习 2**：`Workspace` 和 `OpBuffer` 都是"非输入输出"的数据，为什么要分成两个消息？

**答案**：`OpBuffer` 描述 L1 等显存 buffer（带 `BufferType`），`Workspace` 描述算子 workspace（当前只有 LOG 类型）。两者在 Python 侧的处理也不同：`'space'` 被当作 workspace 采集项落盘命名，`'buffer'` 不在 `DumpDataParser.parse_types`（input/output/space）中，仅在 `BigDumpDataParser` 做 size 对账时参与字节数累计。

### 4.2 dump_proto_to_json.cpp 与 libascend_dump_parser.so：C++ 侧的解析引擎

#### 4.2.1 概念说明

为什么要用 C++ 解析，而不是直接在 Python 里 `import google.protobuf`？因为 CANN 环境统一携带经过符号隔离（`google=ascend_private` 宏重命名命名空间）的 protobuf 静态库，工具只需编译一次成 so，无需用户额外安装 Python protobuf 包，也避免了与用户环境里其他 protobuf 版本冲突。这也是 `docs/zh/msaicerr/Dump_files_parsing.md` 中"不能识别的 dtype 需用户自装第三方库"仅针对 numpy 侧（如 bfloat16ext）的原因——proto 解析本身零依赖。

#### 4.2.2 核心流程

```
Python 传入 (二进制全文, 长度, json路径)
        │
        ▼
ParseDumpProtoToJson (C++)
        │ 1. 校验 data/paths 非空、dataLength ≥ 8
        │ 2. 前 8 字节 reinterpret_cast 为 uint64 headLength
        │ 3. 校验 dataLength ≥ headLength + 8
        │ 4. protoData = data[8, 8+headLength)
        │ 5. DumpData.ParseFromString(protoData)
        │ 6. MessageToJsonString（枚举输出为整数、保留 proto 字段名）
        │ 7. SaveToFile：realpath 规范化目录后写文件
        ▼
返回 0 表示成功，JSON 落盘在 <dump文件名>.json
```

#### 4.2.3 源码精读

[dump_proto_to_json.h:22-29](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/dump_proto_to_json.h#L22-L29)
—— 头文件用 `extern "C"` 包裹并加 `__attribute__((visibility("default")))` 导出唯一的 C 链接函数 `ParseDumpProtoToJson(const char*, size_t, const char*)`。**这是 Python 能按名字找到它的前提**：C++ 有名字改编（name mangling），extern "C" 保证符号名就是 `ParseDumpProtoToJson`。

[dump_proto_to_json.cpp:46-70](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/dump_proto_to_json.cpp#L46-L70)
—— 函数主体：参数与长度校验后，第 57 行把数据头部 8 字节重解释为 `uint64_t headLength`，第 63 行切出 header 段 `protoData`，第 66 行 `ParseFromString` 反序列化成 `toolkit::dumpdata::DumpData`。任何一步失败都返回 -1，由 Python 侧统一报 MS_AICERR_CONNECT_ERROR。

[dump_proto_to_json.cpp:71-83](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/dump_proto_to_json.cpp#L71-L83)
—— proto → JSON 的三个关键 `JsonPrintOptions`：`always_print_primitive_fields`（零值字段也输出，Python 才能稳定拿到 size=0 等默认值）、`always_print_enums_as_ints`（dtype 以整数输出，正好匹配 `DATA_TYPE_TO_DTYPE_MAP` 用字符串数字做的键）、`preserve_proto_field_names`（字段名保持 proto 原名如 `data_type`，Python 侧 `item.get('data_type')` 直接可用）。最后 `SaveToFile` 落盘。

[proto_parse/CMakeLists.txt:17-29](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/CMakeLists.txt#L17-L29)
—— 构建第一步：`protobuf_generate` 用 protoc 把 `dump_data.proto` 编译成 `dump_data.pb.h/.pb.cc`（这就是 cpp 里 include 的 `proto/proto_parse/dump_data.pb.h` 的来源）；第二步：`add_library(ascend_dump_parser SHARED ...)` 把生成代码与 `dump_proto_to_json.cpp` 一起编成共享库，CMake 的 SHARED 目标名 `ascend_dump_parser` 在 Linux 上即产出 **`libascend_dump_parser.so`**。

[proto_parse/CMakeLists.txt:39-41](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/CMakeLists.txt#L39-L41) 与 [proto_parse/CMakeLists.txt:58-67](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse/CMakeLists.txt#L58-L67)
—— `google=ascend_private` 编译定义实现 protobuf 符号隔离；一组 `-Wl` 链接选项（RELRO、noexecstack、`--exclude-libs,ALL` 隐藏依赖库符号）保证 so 安全且不污染宿主进程符号表；`install(TARGETS ...)` 把 so 装到 CANN 的 `lib64` 目录，使其能被系统动态链接器找到。

#### 4.2.4 代码实践

1. **实践目标**：确认"库名 → 符号名 → Python 调用名"三者的对应关系。
2. **操作步骤**：
   - 在已安装 CANN 的机器上执行 `ls $ASCEND_HOME_PATH/*/lib64/libascend_dump_parser.so 2>/dev/null || find /usr/local/Ascend -name 'libascend_dump_parser.so'`；
   - 再执行 `nm -D <so路径> | grep ParseDumpProtoToJson`（或 `readelf --dyn-syms <so路径> | grep ParseDump`）。
3. **需要观察的现象**：so 文件存在于 CANN 安装目录的 lib64 下；动态符号表中能看到未改编的 `ParseDumpProtoToJson` 符号。
4. **预期结果**：符号存在，证明 extern "C" + visibility("default") 生效。若本机无 CANN 环境，此步骤**待本地验证**，可退化为纯源码阅读：在 .h 中找到导出声明，在 CMakeLists 中找到 SHARED 目标名即可。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `dump_proto_to_json.h` 里的 `extern "C"` 去掉，Python 侧会发生什么？

**答案**：C++ 编译器会对函数名做 name mangling，导出的符号变成类似 `_Z19ParseDumpProtoToJsonPKcmS0_` 的形式，Python 的 `ctypes.CDLL(...).ParseDumpProtoToJson` 将抛出 `AttributeError: function 'ParseDumpProtoToJson' not found`，工具报 MS_AICERR_CONNECT_ERROR。

**练习 2**：`always_print_enums_as_ints = true` 若改为 false，会破坏 Python 侧哪段代码？

**答案**：JSON 中的 `data_type` 会变成 `"DT_FLOAT16"` 这类字符串而不是 `2`。而 `dump_data_parser.py` 的 `_get_item_dtype` 用 `str(item.get('data_type', '0'))` 构造键去查 `DATA_TYPE_TO_DTYPE_MAP`（键是 '1'、'2' 这类数字字符串），将全部查不到而退化到 json dtype 兜底，dtype 识别大面积失效。

### 4.3 BigDumpDataParser：Python 与 C++ 的 ctypes 桥

#### 4.3.1 概念说明

`BigDumpDataParser`（内部类，同文件 538 行起）负责单个 Dump 文件的"header 解析 + 数据回填"。它是 Python 与 C++ 的**唯一交界**：交互方式是 `ctypes.CDLL` 加载 so 后**直接函数调用**——既不是子进程执行可执行文件，也不是 import Python 扩展模块。大纲规划时曾猜测是"可执行文件"，源码证实是 **CDLL 动态库直调**，这也是本讲实践任务要得出的结论。

#### 4.3.2 核心流程

```
parse()
 ├── check_argument_valid()          # 路径合法性、文件大小 > 8 字节，> 1GB 仅告警
 ├── _parse_dump_to_json()           # ← ctypes 调 C++，header → 临时 .json → dict → 删除 json
 ├── _read_header_length()           # 重开文件读前 8 字节，校验 header_length ≤ file_size - 8
 └── _parse_binary_to_json_data()    # 按 size 顺序从文件读回每个张量的 data 字节
       └── input 条目 input_type == 7 (TILING_TYPE) 时，额外记为 tiling_data
```

字节数对账逻辑：设 `used_size = header_length + 8`，每处理一个条目累加其 `size`，一旦 `used_size > file_size` 即判文件非法（防止越界读）。

#### 4.3.3 源码精读

[dump_data_parser.py:594-614](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L594-L614)
—— **本讲最关键的代码**。第 605 行 `dump_parse_cdll = ctypes.CDLL(self.parse_dump_so)` 加载 `libascend_dump_parser.so`（名字由 548 行 `self.parse_dump_so = "libascend_dump_parser.so"` 给出，不带路径，依赖 source CANN 环境后的 `LD_LIBRARY_PATH`）；第 610-611 行 `res = dump_parse_cdll.ParseDumpProtoToJson(data_ptr, ctypes.c_size_t(len(binary_data)), json_file.encode('utf-8'))` 把**整个文件字节**与目标 json 路径传给 C++。这就是"Python 与 C++ 交互的那几行"——ctypes 动态库直调。

[dump_data_parser.py:615-622](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L615-L622)
—— C++ 返回 0 且 json 文件存在后，Python 用 `json.load` 把临时 JSON 读成 `self.dump_json_data`，随后 `os.remove(json_file)` 立即删除临时文件——JSON 只是 Python 与 C++ 之间的一次性数据交接载体，不留痕。

[dump_data_parser.py:624-635](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L624-L635)
—— 数据回填：对 input/output/buffer/space 四类条目按 `size` 从文件继续读字节塞进 `item['data']`，并做 `used_size` 对账防越界；当 `data_type == 'input'` 且 `input_type == Constant.TILING_TYPE`（值为 7，见 [constant.py:374](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/constant.py#L374)）时把这段字节另存为 `self.tiling_data`（供后续单算子复现使用）。

[dump_data_parser.py:637-649](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L637-L649)
—— 用 `struct.unpack('Q', ...)` 读前 8 字节得到 header_length 并校验其不超过 `file_size - 8`。注意这与 C++ 侧第 57 行的解读是**同一份头部、两边各读一次**：C++ 用来切 proto 段，Python 用来跳过 header 定位数据区。

[dump_data_parser.py:571-592](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L571-L592)
—— 前置防御：路径校验；文件大小 ≤ 8 字节直接判非法 Dump；> 1GB 打印"耗时较长"告警但不拦截，体现 msaicerr"尽力而为"的气质。

#### 4.3.4 代码实践

1. **实践目标**：亲手复现一次最小的 ctypes 调用链，验证参数编组方式。
2. **操作步骤**（示例代码，非项目代码）：
   ```python
   # 示例代码：模拟 _parse_dump_to_json 的调用形态
   import ctypes, os
   lib = ctypes.CDLL("libascend_dump_parser.so")   # 需在 source CANN 环境后运行
   data = open("exception_info.2.1.xxx", "rb").read()
   ret = lib.ParseDumpProtoToJson(
       ctypes.c_char_p(data),
       ctypes.c_size_t(len(data)),
       b"/tmp/dump_head.json")
   print("ret =", ret, "json exists =", os.path.isfile("/tmp/dump_head.json"))
   ```
   在有 CANN 环境的机器上针对一个真实 Dump 文件运行；无环境则纸面跟踪 `dump_data_parser.py:605-611` 三个实参的类型。
3. **需要观察的现象**：返回值 ret 为 0，`/tmp/dump_head.json` 生成，且内容含 `input`/`output` 数组与 `data_type` 整数。
4. **预期结果**：验证交互方式为 **ctypes.CDLL 动态库直调**（非子进程、非 so 之外的任何形式）。无真实 Dump 文件时**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Python 把**整个文件**（包括张量数据）都传给 C++，而不是只传 header 段？

**答案**：header_length 藏在文件前 8 字节里，切分 header 本来就是 C++ 侧第 57-63 行的职责；Python 若先自己切，就要在 Python 里再实现一遍 `struct.unpack` 与边界校验，职责重复。C++ 侧也用 `dataLength ≥ headLength + 8` 做了防御，超长数据不会引起越界。

**练习 2**：临时 JSON 被删掉了，`self.dump_json_data` 里的 `data` 字段是哪来的？

**答案**：JSON 里各条目的 `data` 一开始是空的（或未打印的默认值），真正的数据字节是 `_parse_binary_to_json_data` 打开原 Dump 文件、按 `size` 顺序读回并写入 `item['data']` 的。JSON 只交接元数据，数据走二进制文件，二者在内存里合并。

### 4.4 DumpDataParser：dtype 映射、落盘与 -dtype 转换

#### 4.4.1 概念说明

`BigDumpDataParser` 产出"带字节数据的 dict"之后，`DumpDataParser` 负责三件事：确定每个张量的 dtype、把数据落成 `.npy` 或 `.bin`、以及处理 `-dtype` 显式转换。dtype 的确定优先级是：**proto 里的 data_type 枚举 > 编译产物 json 文件里的 dtype > 原始枚举值**，三级兜底保证尽量给出可读类型。

#### 4.4.2 核心流程

```
parse()  (dump_data_parser.py:487)
 ├── 入参是 .npy 文件      → 直接报错返回（npy 无需再解析）
 ├── 入参是 .bin 文件      → convert_bin_file_to_npy()，此时 -dtype 才有意义
 ├── 入参是目录            → 按 info.node_name（超长名经 mapping.csv 反查）匹配文件列表
 └── 对每个匹配文件 parse_dump_data()
       ├── BigDumpDataParser().parse()           # 4.3 的 ctypes 链路
       ├── _get_json_dtypes()                    # 从编译产物 json 收集 dtype 兜底信息
       ├── 依次处理 input / output / space 三类：
       │     _get_item_dtype  → _build_typed_array → _build_dst_file_name → _save_array
       │     再附 NaN/INF 检查、极值检查与统计摘要（Max/Min/Mean/Std）
       └── _save_dfx_message()                   # 记录 proto 里的 dfx_message
```

`-dtype` 的作用路径：`msaicerr.py` 的 `-dtype/--dest_dtype` 参数 → `convert_dump_data()` → `DumpDataParser(dest_dtype=...)` → 仅当 `-d` 指向 `.bin` 文件时走 `convert_bin_file_to_npy()`。

#### 4.4.3 源码精读

[msaicerr.py:234-237](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L234-L237)
—— `-dtype` 参数定义：`dest_dtype`，默认空串，`action=RequireOtherArgs, required_args=['data']` 强制它必须与 `-d` 搭配使用（u3-l1 讲过的自定义 Action）。

[msaicerr.py:126-145](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L126-L145)
—— `convert_dump_data()`：校验 `-d` 路径与输出目录（未指定 `-out` 时以 Dump 文件所在目录为输出目录），构造空 `AicErrorInfo` 容器后交给 `DumpDataParser(data_path, info, args.dest_dtype, args.output_path).parse()`。

[dump_data_parser.py:43-98](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L43-L98)
—— `ConstManager` 的三张类型表：`DATA_TYPE_TO_DTYPE_MAP`（proto 枚举号 → dtype 名，覆盖 0~42 号）、`NUMPY_NATIVE_DTYPES`（numpy 原生可表示的 14 种，这些落 `.npy`）、`NUMPY_EXT_DTYPES`（bfloat16，需第三方 `bfloat16ext` 注册后才能被 numpy 表示）。这张表决定了"落 npy 还是落 bin"。

[dump_data_parser.py:270-284](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L270-L284)
—— `_get_item_dtype` 的三级优先级：workspace 恒为 int8；否则查 `DATA_TYPE_TO_DTYPE_MAP`，若得到有效（非 undefined）dtype 直接用；undefined 或未知时回退编译产物 json 里的 dtype，最后才保留原始枚举号。这解释了文档中 "Can not read with dtype xxx" 提示的来源。

[dump_data_parser.py:286-300](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L286-L300)
—— `_build_typed_array`：先按 int8 视角包成 numpy 数组，若 dtype numpy 可表示且字节数能被 itemsize 整除，则 `view(np_dtype)` 换视角、按 shape `reshape`。字节数不对齐时保留 int8 数组、np_dtype 置 None（后续落 `.bin`）。

[dump_data_parser.py:335-353](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L335-L353)
—— 落盘命名与保存：文件名为 `{kernel_name}.{input|output|workspace}.{index}.{dtype}.{npy|bin}`；numpy 可表示的 dtype 用 `np.save` 存 `.npy`，否则 `tofile` 存裸 `.bin`。`_check_file_name_len`（324-333 行）对超过 255 字节（NAME_MAX）的文件名生成随机数字串改名并登记 `mapping.csv`——与 u3-l1 讲过的 SK 超长文件名机制呼应。

[dump_data_parser.py:405-460](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L405-L460)
—— `convert_bin_file_to_npy()`，**`-dtype` 的最终消费点**：未指定 dest_dtype 直接报错；dtype 不在合法表内报错并列出全部合法值；若 `.bin` 文件名里已带 dtype 且与 dest_dtype 不同只告警不拦截，按用户指定的为准；输出 `{basename}.{dest_dtype}.npy`。bfloat16 走特殊四步（int16 读入 → float32 → 按 ±3.3895e+38 截断 → 转 bfloat16）防止上溢段错误，普通 dtype 直接 `np.fromfile(dtype=...)`。

[dump_data_parser.py:492-501](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dump_data_parser.py#L492-L501)
—— `parse()` 的入口分派：`.npy` 拒绝、`.bin` 走转换、目录走按名匹配。**第 499-500 行是 `-dtype` 的边界约束**：如果给的是非 bin 的单文件却带了 dest_dtype，报 "dest_dtype is only valid for bin file conversion" 后直接返回——这就是"`-dtype` 只对 bin 文件生效"的代码出处。

#### 4.4.4 代码实践

1. **实践目标**：用 `-dtype` 把一个 `.bin` 转成 `.npy`，观察输出命名与告警行为。
2. **操作步骤**：
   - 构造一个 8 字节的假 bin（示例代码）：`python3 -c "import numpy as np; np.array([1,2,3,4], dtype=np.float16).tofile('/tmp/demo.float16.bin')"`；
   - 执行 `cd src/msaicerr && python3 msaicerr.py -d /tmp/demo.float16.bin -dtype int32 -out /tmp/out`（需已 source CANN 环境，即 `ASCEND_OPP_PATH` 已设置）；
   - 再故意省略 `-dtype` 执行一次，观察差异。
3. **需要观察的现象**：第一次运行生成 `/tmp/out/demo.int32.npy`（若同时装了 bfloat16ext 等环境则无额外报错）；由于文件名带 `.float16` 而 dest_dtype 是 int32，日志会打印 "Original bin file dtype float16 is different from dest_dtype int32" 的 Info 告警；省略 `-dtype` 时报 "Need to specify the dtype when convert a bin file."。
4. **预期结果**：验证 `-dtype` 的三条规则——必须显式指定、必须是合法 dtype、与原文件名 dtype 冲突时以用户指定为准。无 CANN 环境时**待本地验证**（此时可退化为阅读 405-460 行源码推演上述三条行为）。

#### 4.4.5 小练习与答案

**练习 1**：`asys collect` 采下来的 Dump 目录（含超长文件名被改名的场景）交给 msaicerr 解析时，`parse()` 如何找对文件？

**答案**：当 `dump_path` 是目录时，`parse()` 先 `_load_name_mapping()` 读取 `mapping.csv`，用 `info.data_name or info.node_name` 作为键反查出重命名后的随机数字串（510 行），再用它对目录做子串匹配收集文件列表；跳过 `mapping.csv` 自身（513-514 行）。

**练习 2**：为什么 `_check_tensor_data` 对 bfloat16 要借用 float32 的 `finfo` 来查数值范围？

**答案**：第 159-160 行注释说明 bfloat16 没有 numpy `finfo` 元信息；bfloat16 的动态范围与 float32 相同（同样 8 位指数），只是尾数精度低，因此用 float32 的上下界做"0.9 × 极值"越界告警在数学上等价且安全。

**练习 3**：一个 dtype 为 float8_e4m3fn（枚举 35，Ascend950 新增）的张量，解析后会得到什么产物？

**答案**：`DATA_TYPE_TO_DTYPE_MAP` 能把 35 映射为 'float8_e4m3fn'，但它不在 `NUMPY_NATIVE_DTYPES` 也不在 `NUMPY_EXT_DTYPES`，`_to_numpy_dtype` 返回 None，于是落盘为 `.bin`（`_save_array` 的 tofile 分支），摘要走 `_summary_tensor_without_dtype` 用 COMMON_DTYPE 五种常见 dtype 逐个试探，输出多段 "If dtype is xxx, summary is: ..."。

## 5. 综合实践

**任务：画出一次完整 Dump 解析的数据流图，并标注每一环的代码位置。**

以 `python3 msaicerr.py -d /path/to/exception_info.2.1.xxx -dtype float16`（假设输入是 bin）与不带 `-dtype` 的目录解析两条命令为对象，完成：

1. 从 [msaicerr.py:255-256](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L255-L256) 的分发开始，依次列出经过的函数：`convert_dump_data` → `DumpDataParser.parse` → （bin 路径 `convert_bin_file_to_npy`；目录/裸 Dump 路径 `parse_dump_data` → `BigDumpDataParser.parse` → `_parse_dump_to_json` → **ctypes 调 `ParseDumpProtoToJson`** → `_read_header_length` → `_parse_binary_to_json_data` → `_save_data_to_bin_file` → `_parse_one_item` → `_save_array`）。
2. 在图中标出"Python 领地"与"C++ 领地"的边界，写明穿越边界传递的三样东西：二进制全文、字节长度、临时 JSON 路径。
3. 对每个产物文件（`.npy`/`.bin`/`mapping.csv`），在图上标注其生成代码行号。
4. 最后回答一个问题：如果用户机器上找不到 `libascend_dump_parser.so`，工具会在哪一行、以什么错误码失败？（提示：看 `_parse_dump_to_json` 的 605-608 行与 [constant.py:44](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/constant.py#L44) 的 `MS_AICERR_CONNECT_ERROR = 3`。）

完成后再对照 `docs/zh/msaicerr/Dump_files_parsing.md` 的用户视角描述，确认文档中"输出结果默认与 Dump 文件同目录"对应 `convert_dump_data` 里未传 `-out` 时的分支。

## 6. 本讲小结

- Dump "大文件"是两段式布局：8 字节 header_length + proto 序列化的 `DumpData` 元数据 + 顺序拼接的张量裸数据；`dump_data.proto` 是元数据段的结构声明书。
- C++ 侧 `dump_proto_to_json.cpp` 被 CMake 编成共享库 `libascend_dump_parser.so`，只导出一个 extern "C" 函数 `ParseDumpProtoToJson`，完成 "proto 二进制 → JSON 文件"。
- Python 与 C++ 的交互方式是 **ctypes.CDLL 动态库直调**（`dump_data_parser.py:605-611`）：传整个文件字节与临时 JSON 路径，读完即删——不是子进程，也不是 Python protobuf 包。
- 张量数据本体不经过 proto/JSON：Python 侧按 header 中声明的 `size` 顺序从文件读回，`used_size` 对账防越界；tiling 输入（input_type=7）被单独摘出。
- dtype 判定三级兜底（proto 枚举 > 编译产物 json > 原始枚举号），numpy 可表示的落 `.npy`，其余落 `.bin` 并用常见 dtype 试探出统计摘要。
- `-dtype` 仅在 `-d` 指向 `.bin` 文件时生效，按用户指定 dtype 生成 `{basename}.{dtype}.npy`，与文件名中的原 dtype 冲突时只告警并以用户为准。

## 7. 下一步学习建议

- 下一讲 [u3-l4] 讲 msaicerr 的环境检查（`dsmi_interface.py` 与 `utils.py`），补齐 `-e` 模式的最后一块拼图。
- 建议延伸阅读：`src/msaicerr/ms_interface/aicore_error_parser.py` 中 `DumpDataParser` 的另一处调用（1458 行附近），看 `-p` 模式的 AI Core Error 流水线如何在 Step 中内嵌本讲的 Dump 解析能力；以及 `operator_cmp/compare` 目录（`parse_dump_data` 中被加入 `sys.path`），了解精度比对工具如何复用同一套解析产物。
